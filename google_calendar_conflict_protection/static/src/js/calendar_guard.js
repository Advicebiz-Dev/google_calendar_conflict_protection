/** @odoo-module **/

/**
 * Odoo 19 calendar view. A single click refreshes ONE event before opening
 * its details popover. A double click deliberately follows the same path:
 * editing only happens after the explicit Edit button in the popover.
 * Server-side safeguards remain authoritative for edits, drops and deletes.
 */
import { patch } from '@web/core/utils/patch';
import { CalendarModel } from '@web/views/calendar/calendar_model';
import { CalendarController } from '@web/views/calendar/calendar_controller';
import { CalendarCommonRenderer } from '@web/views/calendar/calendar_common/calendar_common_renderer';
import { _t } from '@web/core/l10n/translation';

function notify(env, message, type = 'warning') {
    env.services.notification.add(message, {
        title: _t('Atualização de reunião'),
        type,
        sticky: type === 'warning',
    });
}

patch(CalendarModel.prototype, {
    async load(...args) {
        const result = await super.load(...args);
        if (this.resModel !== 'calendar.event') {
            return result;
        }
        let ids = Object.values(this.records || {})
            .map((record) => record.id)
            .filter((id) => Number.isInteger(id) && id > 0);
        // A declined invitation should not remain struck through in the
        // invited user's agenda. Hide only the visual record; never archive or
        // unlink its real calendar.event, so re-acceptance restores it.
        if (ids.length) {
            try {
                const declined = await this.orm.call(
                    'calendar.event', 'gc_guard_declined_ids', [ids]
                );
                for (const id of declined || []) {
                    delete this.data.records[id];
                }
                if (declined?.length) {
                    ids = ids.filter((id) => !declined.includes(id));
                    this.notify();
                }
            } catch (error) {
                // Keep the native view if the optional visibility query fails;
                // never delete or archive actual event records.
                notify(this.env, _t('Não foi possível atualizar a visibilidade dos convites recusados.'));
            }
        }
        this.gcGuardVersions = {};
        if (ids.length) {
            try {
                this.gcGuardVersions = await this.orm.call(
                    'calendar.event', 'gc_guard_versions', [ids]
                );
            } catch (error) {
                // Without baselines, a drag should never be accepted.
                this.gcGuardVersions = null;
                notify(this.env, _t('Não foi possível verificar as versões das reuniões. O arrastamento está temporariamente bloqueado.'));
            }
        }
        // A conflict has two visual entries, but only one real calendar.event.
        // The negative id is a client-only preview: it never touches the ORM.
        if (ids.length) {
            try {
                const snapshots = await this.orm.call('calendar.event', 'gc_guard_calendar_previews', [ids]);
                const nameKey = this.meta.fieldMapping.create_name_field || 'display_name';
                const startField = this.meta.fieldMapping.date_start;
                const stopField = this.meta.fieldMapping.date_stop;
                for (const [originalId, copy] of Object.entries(snapshots || {})) {
                    const current = this.records[originalId];
                    if (!current || copy.state === 'cancelled' || !copy.start || !copy.stop) {
                        continue;
                    }
                    const virtualId = -Number(originalId);
                    const start = copy.allday ? copy.start.slice(0, 10) : copy.start;
                    const stop = copy.allday ? copy.stop.slice(0, 10) : copy.stop;
                    const raw = {
                        ...current.rawRecord,
                        id: virtualId,
                        [nameKey]: `⚠ Google (conflito): ${copy.name || current.title}`,
                        [startField]: start,
                        ...(stopField ? { [stopField]: stop } : {}),
                    };
                    if (this.meta.fieldMapping.all_day) {
                        raw[this.meta.fieldMapping.all_day] = !!copy.allday;
                    }
                    const alternative = this.normalizeRecord(raw);
                    alternative.gcGuardVirtual = true;
                    alternative.gcGuardMasterId = Number(originalId);
                    alternative.title = raw[nameKey];
                    this.data.records[virtualId] = alternative;
                    current.title = `⚠ Odoo (conflito): ${current.title}`;
                    current.gcGuardConflict = true;
                }
                if (Object.keys(snapshots || {}).length) {
                    this.notify();
                }
            } catch (error) {
                notify(this.env, _t('Não foi possível obter as versões alternativas em conflito. Consulte o formulário da reunião.'));
            }
        }
        // Separate REAL events at overlapping times are schedule conflicts.
        // They must each keep their own title and period (no virtual copies).
        if (ids.length) {
            try {
                const overlaps = await this.orm.call(
                    'calendar.event', 'gc_guard_calendar_overlaps', [ids]
                );
                this.gcGuardOverlaps = overlaps || {};
                for (const [id, matches] of Object.entries(this.gcGuardOverlaps)) {
                    const entry = this.records[id];
                    if (!entry || !matches.length) {
                        continue;
                    }
                    entry.gcGuardScheduleConflict = true;
                    entry.title = `⚠ Conflito de horário: ${entry.title}`;
                }
                if (Object.keys(this.gcGuardOverlaps).length) {
                    this.notify();
                }
            } catch (error) {
                this.gcGuardOverlaps = null;
                notify(this.env, _t('Não foi possível analisar as reuniões sobrepostas.'));
            }
        }
        return result;
    },

    async updateRecord(record, options = {}) {
        if (this.resModel !== 'calendar.event' || !record.id) {
            return super.updateRecord(record, options);
        }
        if (record.gcGuardVirtual || record.id < 0) {
            notify(this.env, _t('Esta é apenas a versão Google preservada para comparação. Resolva o conflito no evento original.'));
            await this.load();
            return;
        }
        const before = this.gcGuardVersions?.[record.id];
        if (!before) {
            await this.load();
            notify(this.env, _t('Não foi possível confirmar a versão original da reunião. Volte a tentar.'));
            return;
        }
        try {
            const result = await this.orm.call(
                'calendar.event', 'gc_guard_preflight', [[record.id], before]
            );
            if (result.status !== 'ok') {
                await this.load();
                notify(this.env, _t('O movimento não foi aplicado. ') + (result.message || _t('A reunião foi alterada entretanto.')));
                return;
            }
            const vals = this.buildRawRecord(record, options);
            delete vals.name;
            await this.orm.write('calendar.event', [record.id], vals, {
                context: {
                    ...this.meta.context,
                    gc_expected_version: result.version,
                    gc_expected_etag: result.etag,
                },
            });
            await this.load();
        } catch (error) {
            await this.load();
            throw error;
        }
    },

    async unlinkRecord(recordId) {
        if (Number(recordId) < 0) {
            notify(this.env, _t('A pré-visualização de conflito não pode ser eliminada.'));
            return;
        }
        try {
            return await super.unlinkRecord(recordId);
        } catch (error) {
            await this.load();
            throw error;
        }
    },
});

// The native single click opens the popover without validating the event;
// the native double click opens the editor directly. Guard both entry points.
patch(CalendarCommonRenderer.prototype, {
    async onClick(info) {
        if (this.props.model.resModel !== 'calendar.event') {
            return super.onClick(info);
        }
        const model = this.props.model;
        const recordId = Number(info.event.id);
        if (recordId < 0) {
            await this.env.services.action.doAction({
                type: 'ir.actions.act_window', res_model: 'calendar.event',
                res_id: -recordId, view_mode: 'form',
                views: [[false, 'form']], target: 'new',
            });
            return;
        }
        if (!Number.isInteger(recordId) || recordId <= 0 || this._gcClickChecking) {
            return;
        }
        this._gcClickChecking = true;
        this.popover.close();
        try {
            // Even for a recent-looking event, ALWAYS ask the server to
            // refresh this specific event from Google before displaying it.
            const result = await this.env.services.orm.call(
                'calendar.event',
                'gc_guard_preflight',
                [[recordId], model.gcGuardVersions?.[recordId] || false]
            );
            if (['deleted'].includes(result.status)) {
                await model.load();
                notify(this.env, result.message || _t('A reunião foi eliminada no Google Calendar.'));
                return;
            }
            if (['google_changed', 'odoo_changed', 'conflict'].includes(result.status)) {
                await model.load();
                notify(this.env, result.message || _t('A reunião foi alterada entretanto.'));
                if (result.status === 'conflict') {
                    // Expose a form with explicit resolution choices; do not
                    // start normal editing on a conflicting event.
                    await this.env.services.action.doAction({
                        type: 'ir.actions.act_window',
                        res_model: 'calendar.event',
                        res_id: recordId,
                        view_mode: 'form',
                        views: [[false, 'form']],
                        target: 'new',
                    });
                    return;
                }
            }
            // Look up the NEW record after refresh, never use the cached copy.
            const freshRecord = model.records[recordId];
            if (!freshRecord) {
                notify(this.env, _t('O evento já não está visível neste período do calendário.'), 'info');
                return;
            }
            const target = this.fc.api.el.querySelector(`[data-event-id="${recordId}"]`);
            if (!target) {
                notify(this.env, _t('A reunião foi atualizada. Consulte-a no novo horário.'), 'info');
                return;
            }
            this.openPopover(target, freshRecord);
            const event = this.fc.api.getEventById(String(info.event.id));
            if (event) {
                this.highlightEvent(event, 'o_cw_custom_highlight');
            }
        } catch (error) {
            // Do not show potentially stale details when the refresh failed.
            notify(this.env, error.data?.message || error.message || _t('Não foi possível atualizar esta reunião no Google Calendar.'));
        } finally {
            this._gcClickChecking = false;
        }
    },

    convertRecordToEvent(record) {
        const event = super.convertRecordToEvent(record);
        if (record.gcGuardVirtual || record.gcGuardConflict) {
            event.editable = false;
            event.startEditable = false;
            event.durationEditable = false;
        }
        return event;
    },

    eventClassNames(info) {
        const classes = super.eventClassNames(info);
        const record = this.props.model.records[info.event.id];
        if (record?.gcGuardVirtual) {
            classes.push('gc_guard_conflict_google');
        } else if (record?.gcGuardConflict) {
            classes.push('gc_guard_conflict_odoo');
        }
        if (record?.gcGuardScheduleConflict) {
            classes.push('gc_guard_schedule_overlap');
        }
        return classes;
    },

    onDblClick(info) {
        if (this.props.model.resModel !== 'calendar.event') {
            return super.onDblClick(info);
        }
        // A double click must NEVER bypass the first-click refresh.
        return this.onClick(info);
    },
});

patch(CalendarController.prototype, {
    async editRecord(record, context = {}) {
        if (this.model.resModel !== 'calendar.event' || !record.id) {
            return super.editRecord(record, context);
        }
        if (record.gcGuardVirtual || record.id < 0) {
            notify(this.env, _t('Escolha uma das versões no formulário do evento original.'));
            return;
        }
        const version = this.model.gcGuardVersions?.[record.id];
        if (!version) {
            await this.model.load();
            notify(this.env, _t('O calendário foi atualizado. Volte a abrir a reunião para garantir a versão correta.'));
            return;
        }
        const result = await this.orm.call(
            'calendar.event', 'gc_guard_preflight', [[record.id], version]
        );
        if (result.status !== 'ok') {
            await this.model.load();
            notify(this.env, result.message || _t('A reunião mudou. Abra-a novamente antes de editar.'));
            if (result.status === 'conflict') {
                await this.env.services.action.doAction({
                    type: 'ir.actions.act_window', res_model: 'calendar.event',
                    res_id: record.id, view_mode: 'form',
                    views: [[false, 'form']], target: 'new',
                });
            }
            return;
        }
        return super.editRecord(record, {
            ...context,
            gc_expected_version: result.version,
            gc_expected_etag: result.etag,
        });
    },
});
