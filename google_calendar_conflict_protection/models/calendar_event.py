# -*- coding: utf-8 -*-
"""Optimistic conflict checks for Odoo 19 Google Calendar events.

IMPORTANT: test in a clone of the customer's exact Odoo Enterprise build.
No Google request is made during a calendar listing; just before edit/save/drop.
"""
import json
import logging
from datetime import datetime, timedelta

import pytz
from dateutil.parser import parse
from requests import HTTPError, RequestException
from markupsafe import escape

from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError
from odoo.addons.google_calendar.models.google_sync import after_commit
from odoo.addons.google_calendar.utils.google_calendar import GoogleCalendarService

_logger = logging.getLogger(__name__)


class CalendarEvent(models.Model):
    _inherit = 'calendar.event'

    # The ETag is recorded after a successful GET, Google->Odoo import, or PATCH.
    gc_guard_etag = fields.Char(string='Last checked Google ETag', copy=False)
    # Google cancellations are archived locally, not unlinked.  Keep the
    # Google ID and this marker to restore the SAME event if it reappears.
    # It must be a stored field: both the periodic importer and click refresh
    # read/write it across transactions.
    gc_guard_remote_archived = fields.Boolean(
        string='Arquivado por cancelamento no Google', default=False,
        copy=False, readonly=True, index=True,
    )
    gc_guard_conflict = fields.Boolean(string='Conflito de sincronização', default=False, copy=False, readonly=True)
    gc_guard_conflict_message = fields.Text(string='Detalhes do conflito', copy=False, readonly=True)
    gc_guard_remote_etag = fields.Char(string='ETag Google em conflito', copy=False, readonly=True)
    # Two read-only snapshots, NOT two calendar.event records. This avoids
    # duplicate calendar invitations, reminders, appointments and Google inserts.
    gc_guard_odoo_snapshot = fields.Text(string='Versão preservada do Odoo', copy=False, readonly=True)
    gc_guard_google_snapshot = fields.Text(string='Versão preservada do Google', copy=False, readonly=True)
    gc_guard_odoo_preview = fields.Html(string='Versão Odoo', compute='_gc_compute_conflict_previews', sanitize=True)
    gc_guard_google_preview = fields.Html(string='Versão Google', compute='_gc_compute_conflict_previews', sanitize=True)

    # Distinct real appointments with overlapping periods are NOT sync versions.
    # These fields are non-stored so a reschedule removes the warning immediately.
    gc_guard_schedule_conflict = fields.Boolean(
        string='Conflito de horário', compute='_gc_compute_schedule_conflict')
    gc_guard_schedule_preview = fields.Html(
        string='Reuniões em conflito de horário',
        compute='_gc_compute_schedule_conflict', sanitize=True)


    gc_guard_last_push_at = fields.Datetime(string='Último envio confirmado ao Google', copy=False, readonly=True)
    # Protect Google changes soon after a confirmed Odoo push (stale Google browser).
    _gc_guard_concurrency_window = timedelta(minutes=2)
    gc_guard_last_source = fields.Selection(
        [('google', 'Google Calendar'), ('odoo', 'Odoo')],
        default='odoo', copy=False,
    )

    def _gc_local_snapshot(self):
        self.ensure_one()
        return {
            'name': self.name or '',
            'start': fields.Datetime.to_string(self.start) if self.start else False,
            'stop': fields.Datetime.to_string(self.stop) if self.stop else False,
            'location': self.location or '',
            'description': tools.html2plaintext(self.description or '').strip(),
            'allday': bool(self.allday),
            'state': 'active',
        }

    def _gc_remote_snapshot(self, gevent):
        self.ensure_one()
        if gevent.is_cancelled():
            return {'name': gevent.summary or '(Sem título)', 'start': False, 'stop': False,
                    'location': '', 'description': '', 'allday': False, 'state': 'cancelled'}
        all_day = bool(gevent.start.get('date'))
        start = self._gc_google_datetime(gevent.start, all_day)
        stop = self._gc_google_datetime(gevent.end, all_day, is_end=True)
        return {
            'name': gevent.summary or '(Sem título)',
            'start': fields.Datetime.to_string(start),
            'stop': fields.Datetime.to_string(stop),
            'location': gevent.location or '',
            'description': tools.html2plaintext(gevent.description or '').strip(),
            'allday': all_day,
            'state': 'active',
        }

    @api.depends('gc_guard_conflict', 'gc_guard_odoo_snapshot', 'gc_guard_google_snapshot', 'user_id')
    def _gc_compute_conflict_previews(self):
        for record in self:
            record.gc_guard_odoo_preview = False
            record.gc_guard_google_preview = False
            # Never expose a private Google snapshot to colleagues via a calendar
            # event on which they have only free/busy visibility.
            if not record.gc_guard_conflict or record.user_id != self.env.user:
                continue
            for source, field in (('odoo', 'gc_guard_odoo_preview'), ('google', 'gc_guard_google_preview')):
                try:
                    values = json.loads(record['gc_guard_%s_snapshot' % source] or '{}')
                except (TypeError, ValueError):
                    values = {}
                if not values:
                    record[field] = '<p>Versão ainda não disponível.</p>'
                    continue
                rows = []
                for key, label in (('name', 'Assunto'), ('start', 'Início'),
                                   ('stop', 'Fim'), ('location', 'Localização'),
                                   ('description', 'Descrição')):
                    value = values.get(key) or '—'
                    if key in ('start', 'stop') and values.get(key):
                        try:
                            dt = fields.Datetime.to_datetime(values[key])
                            value = fields.Datetime.context_timestamp(record, dt).strftime('%d/%m/%Y %H:%M')
                        except (TypeError, ValueError):
                            pass
                    rows.append('<tr><th class="pe-2">%s</th><td>%s</td></tr>' % (escape(label), escape(str(value))))
                if values.get('state') == 'cancelled':
                    rows.insert(0, '<tr><th>Estado</th><td>Eliminado no Google</td></tr>')
                record[field] = '<table class="table table-sm">%s</table>' % ''.join(rows)

    def _gc_overlapping_events(self):
        """Actual, separate meetings of the current user occupying the same time.

        Do NOT match events by title, timestamp, or creation proximity. Only the
        Google ID identifies two representations of *one* synchronized event.
        A time overlap is a different type of conflict and never changes either
        event in Google or Odoo.
        """
        self.ensure_one()
        if (not self.id or not self.active or not self.start or not self.stop
                or self.stop <= self.start or self.show_as == 'free'
                or self.user_id != self.env.user):
            return self.env['calendar.event']
        return self.search([
            ('id', '!=', self.id),
            ('user_id', '=', self.user_id.id),
            ('active', '=', True),
            ('show_as', '!=', 'free'),
            ('start', '<', self.stop),
            ('stop', '>', self.start),
        ], order='start, id')

    @staticmethod
    def _gc_display_local_time(record, dt):
        if not dt:
            return '—'
        return fields.Datetime.context_timestamp(record, dt).strftime('%d/%m/%Y %H:%M')

    @api.depends('name', 'start', 'stop', 'user_id', 'active', 'show_as')
    def _gc_compute_schedule_conflict(self):
        for rec in self:
            rec.gc_guard_schedule_conflict = False
            rec.gc_guard_schedule_preview = False
            overlaps = rec._gc_overlapping_events()
            if not overlaps:
                continue
            rec.gc_guard_schedule_conflict = True
            # Always show the actual Odoo name/time for EACH real meeting.
            meetings = (rec | overlaps).sorted(key=lambda event: (event.start, event.id))
            rows = []
            for meeting in meetings:
                source = ('Google Calendar' if meeting.gc_guard_last_source == 'google'
                          else 'Odoo' if meeting.gc_guard_last_source == 'odoo'
                          else 'Origem não confirmada')
                rows.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                    escape(meeting.name or '(Sem título)'),
                    escape(rec._gc_display_local_time(meeting, meeting.start)),
                    escape(rec._gc_display_local_time(meeting, meeting.stop)),
                    escape(source),
                ))
            rec.gc_guard_schedule_preview = (
                '<table class="table table-sm"><thead><tr>'
                '<th>Reunião</th><th>Início</th><th>Fim</th><th>Última origem</th>'
                '</tr></thead><tbody>%s</tbody></table>' % ''.join(rows)
            )

    @api.model
    def gc_guard_calendar_overlaps(self, ids):
        """Return schedule clashes for REAL meetings; does not fabricate events.

        This only reveals meetings owned by the current user, and all records
        are fetched through normal ORM access rules (no sudo).
        """
        records = self.browse(ids).exists()
        records.check_access('read')
        own = records.filtered(lambda e: e.user_id == self.env.user and e.active
                               and e.start and e.stop and e.start < e.stop
                               and e.show_as != 'free')
        if not own:
            return {}
        start = min(e.start for e in own)
        stop = max(e.stop for e in own)
        candidates = self.search([
            ('user_id', '=', self.env.user.id),
            ('active', '=', True), ('show_as', '!=', 'free'),
            ('start', '<', stop), ('stop', '>', start),
        ])
        result = {}
        for event in own:
            overlap = candidates.filtered(lambda other: other.id != event.id
                                          and other.start < event.stop and other.stop > event.start)
            if overlap:
                result[event.id] = [{
                    'id': other.id,
                    'name': other.name or '(Sem título)',
                    'start': fields.Datetime.to_string(other.start),
                    'stop': fields.Datetime.to_string(other.stop),
                } for other in overlap]
        return result

    def _gc_mark_conflict(self, message, remote_etag=False, gevent=None):
        """Persist both snapshots without creating additional live meetings."""
        self.ensure_one()
        values = {
            'gc_guard_conflict': True,
            'gc_guard_conflict_message': message,
            'gc_guard_remote_etag': remote_etag or False,
        }
        if not self.gc_guard_odoo_snapshot:
            values['gc_guard_odoo_snapshot'] = json.dumps(self._gc_local_snapshot(), ensure_ascii=False)
        if gevent is None:
            try:
                gevent = self._gc_google_event()
            except (UserError, RequestException):
                _logger.warning('Unable to snapshot remote copy of conflicting meeting %s', self.id)
        if gevent is not None and self.google_id and gevent.id != self.google_id:
            raise UserError(_('O identificador do evento Google não corresponde à reunião selecionada. A sincronização foi bloqueada.'))
        if gevent is not None:
            values['gc_guard_google_snapshot'] = json.dumps(self._gc_remote_snapshot(gevent), ensure_ascii=False)
            values['gc_guard_remote_etag'] = gevent.etag or remote_etag or False
        # No synced calendar field is touched, so no outbound Google PATCH.
        self.with_context(gc_guard_internal=True, no_calendar_sync=True, dont_notify=True).write(values)
        _logger.warning('Google/Odoo event conflict id=%s google_id=%s: %s', self.id, self.google_id, message)

    def _gc_clear_conflict(self):
        self.ensure_one()
        if self.gc_guard_conflict:
            self.with_context(gc_guard_internal=True, no_calendar_sync=True, dont_notify=True).write({
                'gc_guard_conflict': False,
                'gc_guard_conflict_message': False,
                'gc_guard_remote_etag': False,
                'gc_guard_odoo_snapshot': False,
                'gc_guard_google_snapshot': False,
            })

    @api.model
    def gc_guard_calendar_previews(self, ids):
        """Only the event owner can see alternative Google versions."""
        records = self.browse(ids).exists()
        records.check_access('read')
        output = {}
        for rec in records:
            if rec.gc_guard_conflict and rec.user_id == self.env.user and rec.gc_guard_google_snapshot:
                try:
                    output[rec.id] = json.loads(rec.gc_guard_google_snapshot)
                except (ValueError, TypeError):
                    _logger.warning('Invalid Google conflict snapshot for calendar.event %s', rec.id)
        return output

    def _gc_recent_odoo_push(self, google_updated=None):
        self.ensure_one()
        if not self.gc_guard_last_push_at:
            return False
        # Google changes are fetched only every five minutes. Compare Google's
        # event update timestamp to the last successful Odoo push, NOT the time
        # the periodic cron happens to notice the change.
        if google_updated:
            try:
                updated = parse(google_updated).astimezone(pytz.utc).replace(tzinfo=None)
            except (ValueError, TypeError):
                # No trustworthy remote update time: retain both versions.
                return True
            delta = updated - self.gc_guard_last_push_at
            return timedelta(seconds=-30) <= delta <= self._gc_guard_concurrency_window
        return fields.Datetime.now() - self.gc_guard_last_push_at < self._gc_guard_concurrency_window

    def _gc_refresh_guest_response(self, gevent, syncing_user=None):
        """Synchronize the invited user's RSVP independently from event fields.

        An RSVP is not an edit to the meeting's subject/time and must not be
        overridden by the 'last updated wins' comparison on calendar.event.
        Only trust the attendee who is marked ``self`` by the Google account
        used for this GET/sync (or explicitly matches that user's email).
        Never modify another participant's response or send an invitation.
        """
        self.ensure_one()
        if not self.google_id or not gevent or gevent.is_cancelled():
            return False
        if gevent.organizer and gevent.organizer.get('self'):
            return False  # This Google account is the organiser, not a guest.
        user = syncing_user or self.env.user
        if not user or not user.partner_id:
            return False
        google_attendees = gevent.attendees or []
        valid_states = {'accepted', 'declined', 'tentative', 'needsAction'}
        candidate = next((a for a in google_attendees
                          if a.get('self') and a.get('responseStatus') in valid_states), None)
        if not candidate:
            user_emails = {
                tools.email_normalize(email)
                for email in (user.email, user.partner_id.email)
                if email
            }
            candidate = next((a for a in google_attendees
                              if tools.email_normalize(a.get('email')) in user_emails
                              and a.get('responseStatus') in valid_states), None)
        if not candidate:
            return False
        # Never infer a local attendee from the Google organiser/other guests.
        # Use the Odoo *user's* partner rather than another contact that happens
        # to own the same email alias: current_status uses that exact partner.
        local = self.attendee_ids.filtered(lambda a: a.partner_id == user.partner_id)[:1]
        new_state = candidate['responseStatus']
        if not local:
            # This response belongs to the authenticated Google calendar (self)
            # or explicitly matches the synced user's address. Reconstruct only
            # that single missing attendee without updating unrelated guests.
            local = self.env['calendar.attendee'].sudo().with_context(
                gc_guard_internal=True, no_calendar_sync=True,
                dont_notify=True, no_mail_to_attendees=True,
            ).create({
                'event_id': self.id,
                'partner_id': user.partner_id.id,
                'state': new_state,
            })
            _logger.info('Restored missing Google guest attendee for event %s: %s',
                         self.id, local.id)
            return True
        if local.state == new_state:
            return False
        old_state = local.state
        local.sudo().with_context(
            gc_guard_internal=True,
            no_calendar_sync=True,
            dont_notify=True,
            no_mail_to_attendees=True,
            tracking_disable=True,
        ).write({'state': new_state})
        _logger.info('Google RSVP updated: event=%s attendee=%s %s -> %s',
                     self.id, local.id, old_state, new_state)
        return True

    @api.model
    def gc_guard_declined_ids(self, ids):
        """Hide invitations declined by the current user in calendar UI.

        No event archive / Google id detachment: an accepted RSVP must restore
        visibility on the next sync. Do not rely on calendar.event.user_id to
        identify a Google organizer: external invitations can have the local
        synced user as their Odoo owner. Only check the current user's own
        attendee response; never affect other users or appointments.
        """
        events = self.browse(ids).exists()
        events.check_access('read')
        partner = self.env.user.partner_id
        return [event.id for event in events
                if event.google_id
                and any(a.partner_id == partner and a.state == 'declined'
                        for a in event.attendee_ids)]

    def _sync_google2odoo(self, google_events, write_dates=None, default_reminders=()):
        """Never import Google over an unsent Odoo edit or a recent Odoo push.

        Odoo's upstream logic is 'last updated wins'. Filtering BEFORE calling it
        is vital: otherwise Google can replace an Odoo change waiting for an
        after-commit request or one saved in the last two minutes.
        """
        if not google_events:
            return super()._sync_google2odoo(google_events, write_dates=write_dates, default_reminders=default_reminders)
        remote_existing = google_events.exists(self.env)  # populate Odoo ID mapping
        write_dates = dict(write_dates or {})
        blocked_google_ids = set()
        for remote in remote_existing:
            record_id = remote.odoo_id(self.env)
            record = self.browse(record_id).exists()
            if not record or not record.google_id or record.recurrency or record.recurrence_id:
                continue
            # Serialize with concurrent form/drop/Appointments writes.
            self.env.cr.execute('SELECT id FROM calendar_event WHERE id = %s FOR UPDATE', (record.id,))
            record.invalidate_recordset([
                'need_sync', 'gc_guard_etag', 'gc_guard_last_push_at',
                'gc_guard_conflict', 'gc_guard_conflict_message', 'gc_guard_remote_etag',
            ])
            # A Google tombstone is an actual deletion, not an editing conflict.
            # Never block it because of need_sync, a recent Odoo push or a
            # previously registered conflict. The native importer will call
            # _cancel(), which this module handles without an outbound DELETE.
            if remote.is_cancelled():
                continue
            # A remote cancellation is reversible. If an invitation is restored,
            # reactivate exactly the same linked Odoo record rather than creating
            # another meeting or leaving it permanently archived.
            if record.gc_guard_remote_archived:
                record.sudo().with_context(
                    gc_guard_internal=True, no_calendar_sync=True,
                    dont_notify=True, tracking_disable=True,
                ).write({
                    'active': True, 'gc_guard_remote_archived': False,
                    'need_sync': False, 'gc_guard_etag': remote.etag,
                    'gc_guard_last_source': 'google',
                })
                # Restoring 'active' just advanced Odoo write_date. Force the
                # upstream importer to import the *current* Google contents,
                # rather than incorrectly treating that local timestamp as a
                # newer version of the meeting.
                write_dates[record.id] = False
            remote_etag = remote.etag
            # Google may update metadata/ETag after an Odoo push without any
            # relevant meeting-field difference. Never fabricate two equal
            # snapshots as an alleged sync conflict.
            etag_changed = bool(record.gc_guard_etag and remote_etag != record.gc_guard_etag)
            meaningful_change = bool(record._gc_comparison(remote))
            if record.gc_guard_conflict:
                blocked_google_ids.add(remote.id)
                # Keep the remote preview current without ever importing it over Odoo.
                if record.gc_guard_remote_etag != remote_etag or not record.gc_guard_google_snapshot:
                    record._gc_mark_conflict(record.gc_guard_conflict_message or _(
                        'Conflito entre duas versões desta reunião.'
                    ), remote_etag, remote)
            elif record.need_sync:
                blocked_google_ids.add(remote.id)
                if etag_changed and meaningful_change:
                    record._gc_mark_conflict(_(
                        'Existem alterações no Odoo ainda não confirmadas no Google e a versão '
                        'do Google também mudou. As duas versões foram preservadas. Escolha '
                        'qual manter no formulário da reunião.'
                    ), remote_etag, remote)
            elif etag_changed and meaningful_change and record._gc_recent_odoo_push(remote.updated):
                blocked_google_ids.add(remote.id)
                record._gc_mark_conflict(_(
                    'O Google alterou esta reunião pouco depois de uma atualização enviada '
                    'pelo Odoo. A alteração do Odoo foi preservada para evitar perda de dados. '
                    'Escolha qual versão manter no formulário da reunião.'
                ), remote_etag, remote)
        safe_events = google_events.filter(lambda remote: remote.id not in blocked_google_ids)
        # Context separates a cancellation RECEIVED from Google from a local
        # attendee decline or a user-initiated cancellation.
        synced = super(CalendarEvent, self.with_context(
            gc_guard_remote_cancel=True,
        ))._sync_google2odoo(safe_events, write_dates=write_dates, default_reminders=default_reminders)
        # Force the CURRENT Google guest RSVP independently of Odoo event
        # write_date, including when a conflict blocked the meeting-field import.
        for remote in remote_existing:
            if remote.is_cancelled():
                continue
            meeting = self.browse(remote.odoo_id(self.env)).exists()
            if meeting and meeting.google_id == remote.id:
                meeting._gc_refresh_guest_response(remote, syncing_user=self.env.user)
        return synced

    def _gc_hide_google_cancelled(self):
        """Remove confirmed Google-cancelled events from the visible calendar.

        Archive rather than SQL-delete: Appointment links, chatter and audit
        history stay intact. We MUST retain google_id, otherwise a guest who
        re-accepts (or an organiser who restores an event) cannot be matched
        to this Odoo record again. need_sync=False and no_calendar_sync prevent
        sending any cancellation back to Google.
        """
        records = self.exists().filtered(
            lambda e: e.active or (e.google_id and not e.gc_guard_remote_archived)
        )
        if not records:
            return
        records.sudo().with_context(
            gc_guard_internal=True,
            no_calendar_sync=True,
            dont_notify=True,
            tracking_disable=True,
            skip_event_permission=True,
        ).write({
            'active': False,
            'need_sync': False,
            'gc_guard_remote_archived': True,
            'gc_guard_conflict': False,
            'gc_guard_conflict_message': False,
            'gc_guard_remote_etag': False,
            'gc_guard_google_snapshot': False,
            'gc_guard_odoo_snapshot': False,
            'gc_guard_etag': False,
            'gc_guard_last_source': 'google',
        })
        _logger.info('Archived %d Google-cancelled Odoo meetings: %s', len(records), records.ids)

    def _cancel(self):
        """Google-origin cancellation must not become merely 'declined'.

        Odoo's calendar.event._cancel() declines the current attendee when
        the event organiser is someone else, leaving a crossed-out meeting.
        Only bypass that behaviour for confirmed deletions imported from
        Google; regular manual declines keep native Odoo semantics.
        """
        if self.env.context.get('gc_guard_remote_cancel'):
            self._gc_hide_google_cancelled()
            return
        return super()._cancel()

    @api.model
    def _create_from_google(self, gevents, vals_list):
        # The native Google importer provides one vals dict per GoogleEvent.
        # A new Google event is an independent real meeting, not a second
        # version of an unrelated Odoo meeting at a similar date/time.
        vals_list = [dict(vals,
                          gc_guard_last_source='google',
                          gc_guard_etag=gevent.etag or False,
                          need_sync=False)
                     for gevent, vals in zip(gevents, vals_list)]
        return super()._create_from_google(gevents, vals_list)

    def _sync_odoo2google(self, google_service):
        """Never send a known conflict from Odoo during the periodic sync."""
        return super(CalendarEvent, self.filtered(lambda r: not r.gc_guard_conflict))._sync_odoo2google(google_service)

    def _get_post_sync_values(self, request_values, google_values):
        """Remember ETag and push time for events initially created in Odoo."""
        values = super()._get_post_sync_values(request_values, google_values)
        if google_values and google_values.get('etag'):
            values.update({
                'gc_guard_etag': google_values['etag'],
                'gc_guard_last_push_at': fields.Datetime.now(),
                'gc_guard_last_source': 'odoo',
            })
        return values

    def action_gc_guard_accept_google(self):
        """Explicit conflict resolution: accept the current Google version."""
        self.ensure_one()
        self.check_access('write')
        if self.user_id != self.env.user:
            raise UserError(_('Apenas o responsável pelo evento pode resolver este conflito.'))
        if not self.gc_guard_conflict:
            return {'type': 'ir.actions.client', 'tag': 'reload'}
        remote = self._gc_google_event()
        if remote.is_cancelled():
            raise UserError(_('O evento foi eliminado no Google. Resolva a eliminação manualmente.'))
        if self.recurrency or self.recurrence_id:
            raise UserError(_('As reuniões recorrentes exigem resolução manual da série.'))
        owner = self._get_event_user()
        imported = self._odoo_values(remote)
        imported.update({
            'need_sync': False, 'gc_guard_etag': remote.etag,
            'gc_guard_last_source': 'google', 'gc_guard_conflict': False,
            'gc_guard_conflict_message': False, 'gc_guard_remote_etag': False,
            'gc_guard_last_push_at': False,
            'gc_guard_odoo_snapshot': False, 'gc_guard_google_snapshot': False,
        })
        self.with_user(owner).with_context(
            gc_guard_internal=True, no_calendar_sync=True, dont_notify=True,
            skip_contact_description=True,
        )._write_from_google(remote, imported)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_gc_guard_keep_odoo(self):
        """Explicit conflict resolution: schedule a conditional Google update."""
        self.ensure_one()
        self.check_access('write')
        if self.user_id != self.env.user:
            raise UserError(_('Apenas o responsável pelo evento pode resolver este conflito.'))
        if not self.gc_guard_conflict:
            return {'type': 'ir.actions.client', 'tag': 'reload'}
        if self.recurrency or self.recurrence_id:
            raise UserError(_('As reuniões recorrentes exigem resolução manual da série.'))
        remote = self._gc_google_event()
        if remote.is_cancelled():
            raise UserError(_('O evento foi eliminado no Google. Não é seguro recriá-lo automaticamente.'))
        owner = self._get_event_user()
        # The conflict flag remains set until post-commit PATCH succeeds. If the
        # Google ETag changes yet again, If-Match fails and the flag remains.
        self.with_user(owner).with_context(
            gc_guard_manual_resolution=True, gc_expected_etag=remote.etag,
        )._google_patch(GoogleCalendarService(self.env['google.service']), self.google_id, self._google_values(), timeout=8)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _gc_version(self):
        self.ensure_one()
        return fields.Datetime.to_string(self.write_date) if self.write_date else ''

    @api.model
    def gc_guard_versions(self, ids):
        records = self.browse(ids).exists()
        records.check_access('read')
        return {record.id: record._gc_version() for record in records}

    def _gc_google_event(self):
        self.ensure_one()
        if not self.google_id:
            return None
        owner = self._get_event_user()
        token = owner.sudo()._get_google_calendar_token()
        if not token:
            raise UserError(_('Este evento está associado ao Google Calendar, mas não existe um token de acesso válido. A alteração foi bloqueada.'))
        try:
            service = GoogleCalendarService(self.env['google.service'])
            result, _next_token, _reminders = service.get_events(
                event_id=self.google_id, token=token, timeout=8,
            )
            return next(iter(result), None)
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (404, 410):
                raise UserError(_('A reunião não está disponível no Google Calendar (poderá ter sido eliminada). A alteração foi cancelada.')) from exc
            _logger.warning('Google Calendar GET failed for event %s: %s', self.id, exc)
            raise UserError(_('Não foi possível verificar esta reunião no Google Calendar. A alteração foi bloqueada.')) from exc
        except (RequestException, ValueError, TypeError) as exc:
            _logger.warning('Google Calendar GET error for event %s: %s', self.id, exc)
            raise UserError(_('Não foi possível contactar o Google Calendar. A alteração foi bloqueada para prevenir conflitos.')) from exc

    @staticmethod
    def _gc_google_datetime(data, is_all_day, is_end=False):
        if is_all_day:
            # Odoo represents all-day events as 08:00 -> 18:00 (inclusive).
            from datetime import timedelta
            dt = parse(data['date']).replace(hour=18 if is_end else 8)
            if is_end:
                dt -= timedelta(days=1)
            return dt.replace(tzinfo=None)
        return parse(data['dateTime']).astimezone(pytz.utc).replace(tzinfo=None)

    def _gc_comparison(self, gevent):
        self.ensure_one()
        if gevent.is_cancelled():
            return {'Estado': (_('Ativo'), _('Eliminado no Google'))}
        all_day = bool(gevent.start.get('date'))
        start = self._gc_google_datetime(gevent.start, all_day)
        stop = self._gc_google_datetime(gevent.end, all_day, is_end=True)
        comparisons = {
            _('Assunto'): (self.name or '', gevent.summary or '(Sem título)'),
            _('Início (UTC)'): (self.start, start),
            _('Fim (UTC)'): (self.stop, stop),
            _('Localização'): (self.location or '', gevent.location or ''),
            _('Disponibilidade'): (self.show_as or 'busy', 'free' if gevent.is_available() else 'busy'),
            _('Descrição'): (tools.html2plaintext(self.description or '').strip(), tools.html2plaintext(gevent.description or '').strip()),
            _('Dia inteiro'): (bool(self.allday), all_day),
        }
        # Only compare semantically stable fields. Other changes are picked up by ETag
        # after a reference has been established; HTML and attendee payloads differ.
        return {key: (old, new) for key, (old, new) in comparisons.items() if old != new}

    @staticmethod
    def _gc_diff_text(differences):
        if not differences:
            return _('Foram detetadas alterações ao evento.')
        return '\n'.join('%s: %s → %s' % (name, before, after)
                         for name, (before, after) in differences.items())

    def _gc_set_etag(self, etag):
        self.ensure_one()
        if etag and self.gc_guard_etag != etag:
            # A regular ORM write could trigger upstream's immediate Google PATCH
            # when need_sync is already True. This metadata-only update MUST NOT.
            self.env.cr.execute(
                'UPDATE calendar_event SET gc_guard_etag = %s WHERE id = %s',
                (etag, self.id),
            )
            self.invalidate_recordset(['gc_guard_etag'])

    def _gc_read_google(self):
        self.ensure_one()
        gevent = self._gc_google_event()
        if not gevent:
            return None, {}
        return gevent, self._gc_comparison(gevent)

    def gc_guard_preflight(self, expected_version=False):
        """Read the current Google version; do not send *anything* to Google.

        Client calls this before displaying an editor and before a drag/drop write.
        """
        self.ensure_one()
        self.check_access('read')
        if self.gc_guard_conflict and self.google_id and not (self.recurrency or self.recurrence_id):
            # An old unresolved conflict must not keep a meeting visible after
            # the organiser has explicitly deleted it on Google.
            remote = self._gc_google_event()
            if remote and remote.is_cancelled():
                self._gc_hide_google_cancelled()
                return {
                    'status': 'deleted', 'source': 'Google Calendar',
                    'message': _('Esta reunião foi cancelada no Google Calendar e retirada da agenda do Odoo.'),
                    'version': self._gc_version(), 'etag': False,
                }
            if remote:
                self._gc_refresh_guest_response(remote, syncing_user=self._get_event_user())
        if self.gc_guard_conflict:
            if not self.gc_guard_google_snapshot or not self.gc_guard_odoo_snapshot:
                self._gc_mark_conflict(self.gc_guard_conflict_message or _(
                    'Conflito entre duas versões desta reunião.'
                ), self.gc_guard_remote_etag)
            return {
                'status': 'conflict', 'source': _('Odoo / Google Calendar'),
                'message': self.gc_guard_conflict_message or _('Conflito de sincronização por resolver.'),
                'version': self._gc_version(), 'etag': self.gc_guard_remote_etag or self.gc_guard_etag,
            }
        # Even if the local version changed (for example via Appointments),
        # consult Google as part of the click refresh. Return odoo_changed
        # AFTER the remote check so neither source is silently ignored.
        local_changed = bool(expected_version and self._gc_version() != expected_version)
        if not self.google_id:
            if local_changed:
                return {
                    'status': 'odoo_changed', 'source': 'Odoo / Marcações',
                    'message': _('A reunião foi alterada no Odoo (possivelmente através de Marcações). Os dados foram atualizados.'),
                    'version': self._gc_version(), 'etag': False,
                }
            return {'status': 'ok', 'version': self._gc_version(), 'etag': False}
        if self.recurrency or self.recurrence_id:
            return {
                'status': 'conflict', 'source': 'Odoo / Google Calendar',
                'message': _('As reuniões recorrentes não estão protegidas por esta versão do módulo. Para prevenir conflitos, edite esta série diretamente no Google Calendar.'),
                'version': self._gc_version(), 'etag': False,
            }
        gevent, changes = self._gc_read_google()
        remote_etag = gevent.etag
        if gevent.is_cancelled():
            self._gc_hide_google_cancelled()
            return {
                'status': 'deleted', 'source': 'Google Calendar',
                'message': _('Esta reunião foi cancelada no Google Calendar e retirada da agenda do Odoo.'),
                'version': self._gc_version(), 'etag': False,
            }
        response_updated = self._gc_refresh_guest_response(
            gevent, syncing_user=self._get_event_user(),
        )
        if self.need_sync:
            # Local changes not yet acknowledged by Google must NOT be imported
            # simply because their fields differ from Google's older state.
            if not self.gc_guard_etag or remote_etag != self.gc_guard_etag:
                message = _('Odoo e Google Calendar têm versões diferentes enquanto existe uma alteração local pendente. A reunião foi preservada e requer resolução.')
                self._gc_mark_conflict(message, remote_etag, gevent)
                return {
                    'status': 'conflict', 'source': _('Google Calendar / Odoo'),
                    'message': message, 'version': self._gc_version(), 'etag': remote_etag,
                }
            if local_changed:
                return {
                    'status': 'odoo_changed', 'source': _('Odoo / Marcações'),
                    'message': _('A reunião foi atualizada no Odoo enquanto a sincronização de saída estava pendente.'),
                    'version': self._gc_version(), 'etag': remote_etag,
                }
            return {'status': 'ok', 'version': self._gc_version(), 'etag': remote_etag}
        if changes or (self.gc_guard_etag and remote_etag != self.gc_guard_etag):
            # Do not apply a partial Google update over recurring series: those
            # need the normal recurrence-aware synchronizer.
            if self.recurrency or self.recurrence_id:
                return {
                    'status': 'conflict', 'source': 'Google Calendar',
                    'message': _('A reunião recorrente mudou no Google. Atualize a sincronização da série antes de editar no Odoo.'),
                    'version': self._gc_version(), 'etag': remote_etag,
                }
            self.check_access('write')
            owner = self._get_event_user()
            imported_values = self._odoo_values(gevent)
            imported_values.update({'need_sync': False, 'gc_guard_etag': remote_etag, 'gc_guard_last_source': 'google'})
            # Calling the native importer preserves Odoo's transformations.
            # Context avoids writing changes back to Google during this refresh.
            self.with_user(owner).with_context(
                gc_guard_internal=True, no_calendar_sync=True,
                dont_notify=True, skip_contact_description=True,
            )._write_from_google(gevent, imported_values)
            return {
                'status': 'google_changed', 'source': 'Google Calendar',
                'message': _('A reunião foi atualizada a partir do Google Calendar.\n%s') % self._gc_diff_text(changes),
                'version': self._gc_version(), 'etag': remote_etag,
            }
        self._gc_set_etag(remote_etag)
        if response_updated:
            return {
                'status': 'google_changed', 'source': 'Google Calendar',
                'message': _('A resposta ao convite foi atualizada a partir do Google Calendar.'),
                'version': self._gc_version(), 'etag': remote_etag,
            }
        if local_changed:
            return {
                'status': 'odoo_changed', 'source': 'Odoo / Marcações',
                'message': _('A reunião foi alterada no Odoo (possivelmente através de Marcações). O evento foi atualizado antes de continuar.'),
                'version': self._gc_version(), 'etag': remote_etag,
            }
        return {'status': 'ok', 'version': self._gc_version(), 'etag': remote_etag}

    def _gc_check_write(self, vals):
        """Server-side safeguard for Form, calendar drag and Appointments writes.

        Do not trust a client-side refresh to protect the transaction.
        """
        if not (set(vals) & self._get_google_synced_fields()):
            return None
        if vals.get('need_sync') is False or self.env.context.get('gc_guard_internal'):
            return None
        if self.filtered('gc_guard_conflict'):
            raise UserError(_('Esta reunião tem um conflito de sincronização por resolver. Escolha a versão a manter antes de a editar.'))
        # Serialize competing Odoo writers before reading any version snapshot.
        # A row lock also prevents Appointments modifying the event during this check.
        if self.ids:
            self.env.cr.execute(
                'SELECT id FROM calendar_event WHERE id = ANY(%s) ORDER BY id FOR UPDATE',
                (self.ids,),
            )
            self.invalidate_recordset(['write_date', 'gc_guard_etag', 'start', 'stop', 'name', 'location', 'need_sync'])
        etags = {}
        expected_version = self.env.context.get('gc_expected_version')
        requested_etag = self.env.context.get('gc_expected_etag')
        for record in self.filtered('google_id'):
            if record.recurrency or record.recurrence_id:
                raise UserError(_('Por segurança, a edição de eventos Google recorrentes está bloqueada nesta versão do módulo.'))
            if expected_version and (len(self) != 1 or record._gc_version() != expected_version):
                raise UserError(_('Esta reunião foi alterada no Odoo desde que foi aberta. Recarregue-a antes de guardar.'))
            gevent, changes = record._gc_read_google()
            if gevent.is_cancelled():
                raise UserError(_('A reunião foi eliminada no Google Calendar. A alteração não foi guardada.'))
            etag = gevent.etag
            baseline = requested_etag if len(self) == 1 and requested_etag else record.gc_guard_etag
            if baseline and etag != baseline:
                raise UserError(_('A reunião foi alterada no Google Calendar desde a última atualização. Reabra-a para importar a versão mais recente.'))
            if changes and (not baseline or not record.need_sync):
                raise UserError(_('O Google Calendar contém alterações que ainda não estão neste evento do Odoo. Reabra-o para atualizar.\n%s') % record._gc_diff_text(changes))
            etags[record.id] = etag
        return etags

    def write(self, vals):
        if self.env.context.get('gc_guard_internal'):
            return super().write(vals)
        checked = self._gc_check_write(vals)
        if not ('gc_guard_etag' in vals and len(vals) == 1) and vals.get('need_sync') is not False:
            vals = dict(vals, gc_guard_last_source='odoo')
        # We have verified the Google version immediately before the Odoo write.
        # Carry that version into the after-commit sender for If-Match.
        if checked and len(self) == 1:
            etag = checked.get(self.id)
            if etag:
                vals = dict(vals)
                vals['gc_guard_etag'] = etag
                return super(CalendarEvent, self.with_context(gc_expected_etag=etag)).write(vals)
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('gc_guard_internal'):
            for record in self.filtered('google_id'):
                if record.recurrency or record.recurrence_id:
                    raise UserError(_('Por segurança, elimine reuniões recorrentes no Google Calendar e aguarde a sincronização.'))
                # This also checks that Google is reachable.
                gevent, changes = record._gc_read_google()
                if gevent.is_cancelled():
                    raise UserError(_('O evento foi eliminado do Google. Atualize antes de apagar no Odoo.'))
                if record.gc_guard_etag and record.gc_guard_etag != gevent.etag:
                    raise UserError(_('O evento foi modificado no Google. A eliminação no Odoo foi cancelada.'))
                if changes:
                    raise UserError(_('O Google tem uma versão mais recente. A eliminação foi cancelada.'))
                record._gc_set_etag(gevent.etag)
        return super().unlink()

    def _write_from_google(self, gevent, vals):
        return super(CalendarEvent, self.with_context(gc_guard_internal=True, no_calendar_sync=True))._write_from_google(
            gevent, dict(vals, gc_guard_etag=gevent.etag, gc_guard_last_source='google', need_sync=False),
        )

    @after_commit
    def _google_patch(self, google_service, google_id, values, timeout=8):
        """Conditional update; prevents changes between GET and postcommit PATCH.

        This overrides the non-conditional upstream google.calendar.sync._google_patch
        for calendar.event only (not calendar.recurrence).
        """
        record = self.exists()
        if not record:
            return
        record.ensure_one()
        manual_resolution = bool(record.env.context.get('gc_guard_manual_resolution'))
        if record.gc_guard_conflict and not manual_resolution:
            _logger.warning('Google PATCH paused for conflicting event %s', record.id)
            return
        if not record.need_sync and not manual_resolution:
            _logger.info('Google PATCH skipped: event %s is no longer pending', record.id)
            return
        if not manual_resolution and record._google_values() != values:
            _logger.warning('Google PATCH skipped for event %s: local meeting changed after queueing the request', record.id)
            return
        etag = record.env.context.get('gc_expected_etag') or record.gc_guard_etag
        if not etag:
            record._gc_mark_conflict(_('A atualização para Google foi bloqueada por ausência de uma versão de referência (ETag). É necessária resolução manual.'))
            return
        token = record.env.user.sudo()._get_google_calendar_token()
        if not token:
            _logger.error('Google update withheld: no owner token (calendar.event %s)', record.id)
            return
        url = '/calendar/v3/calendars/primary/events/%s?sendUpdates=%s&conferenceDataVersion=1' % (
            google_id, 'none' if record._is_event_over() else 'all',
        )
        headers = {
            'Content-type': 'application/json',
            'Authorization': 'Bearer %s' % token,
            'If-Match': etag,
        }
        try:
            _google_api = GoogleCalendarService(record.env['google.service'])
            _status, response, _elapsed = _google_api.google_service._do_request(
                url, json.dumps(values), headers, method='PATCH', timeout=timeout,
            )
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (409, 412):
                record._gc_mark_conflict(_(
                    'O Google Calendar foi atualizado enquanto o Odoo preparava o envio. '
                    'O envio foi cancelado e a versão do Odoo foi preservada. Resolva '
                    'o conflito no formulário da reunião.'
                ))
            else:
                _logger.exception('Google PATCH error for calendar.event %s', record.id)
            return
        except Exception:
            _logger.exception('Google conditional PATCH failed for calendar.event %s', record.id)
            return
        new_etag = response.get('etag') if isinstance(response, dict) else False
        if new_etag:
            record.with_context(gc_guard_internal=True, no_calendar_sync=True, dont_notify=True).write({
                'gc_guard_etag': new_etag,
                'need_sync': False,
                'gc_guard_conflict': False,
                'gc_guard_conflict_message': False,
                'gc_guard_remote_etag': False,
                'gc_guard_odoo_snapshot': False, 'gc_guard_google_snapshot': False,
                'gc_guard_last_push_at': fields.Datetime.now(),
            })
        else:
            _logger.warning('Google PATCH returned no ETag; keeping need_sync for event %s', record.id)

    @after_commit
    def _google_delete(self, google_service, google_id, timeout=8):
        """Guard remote deletions with If-Match as well."""
        record = self.with_context(active_test=False).exists()
        if not record:
            return
        record.ensure_one()
        etag = record.gc_guard_etag
        token = record.env.user.sudo()._get_google_calendar_token()
        if not etag or not token:
            _logger.error('Google DELETE withheld: missing ETag/token for event %s', record.id)
            return
        try:
            _google_api = GoogleCalendarService(record.env['google.service'])
            _google_api.google_service._do_request(
                '/calendar/v3/calendars/primary/events/%s?sendUpdates=all' % google_id,
                {'access_token': token},
                {'Content-type': 'application/json', 'If-Match': etag},
                method='DELETE', timeout=timeout,
            )
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code not in (404, 410):
                _logger.warning('Google DELETE rejected for event %s: %s', record.id, exc)
                return
        except Exception:
            _logger.exception('Google conditional DELETE failed for event %s', record.id)
            return
        record.with_context(gc_guard_internal=True, no_calendar_sync=True, dont_notify=True).write({'need_sync': False})
