{
    'name': 'AdviceBiz - Google Calendar Conflict Protection',
    'summary': 'Protege a sincronização, identifica sobreposições e sincroniza respostas a convites e protege conflitos',
    'version': '19.0.1.7.0',
    'category': 'Productivity/Calendar',
    'author': 'AdviceBiz',
    'license': 'LGPL-3',
    'depends': ['google_calendar', 'calendar'],
    'data': ['views/calendar_event_views.xml'],
    'assets': {
        'web.assets_backend': [
            'google_calendar_conflict_protection/static/src/js/calendar_guard.js',
            'google_calendar_conflict_protection/static/src/scss/calendar_guard.scss',
        ],
    },
    'installable': True,
    'application': False,
}
