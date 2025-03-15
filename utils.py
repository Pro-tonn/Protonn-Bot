def serverInitTemplate(guild, channels, roles):
    return {"server_id": guild.id,
            "configs": {
                'auto_roles':{
                    'active': False,
                    'roles': []
                },
                'welcome_system': {
                    'active': False,
                    'message': {'title': "Welcome {user}!", 'content': "Welcome to the {server} {user_mention}!\n\nMake sure to read our rules. Hoping you enjoy your stay in {server}", 'thumbnail': '{user}'},
                    'channel': None
                },
                'exit_system': {
                    'active': False,
                    'message': {'title': "Goodbye {user}!", 'content': "{user} has left!\n\nIt is sad to see you go {user_mention}", 'thumbnail': '{user}'},
                    'channel': None
                },
                'ban_system': {
                    'active': False,
                    'message': {'title': "Ban", 'content': "{user} has been struck with the ban hammer",'reason':'', 'thumbnail': '{user}'},
                    'channel': None
                },
                'reaction_roles': {
                    'active': False,
                    'sent': True,
                    'content': {'title': "Reaction Roles", 'type':'select', 'description': "React to the emojis below to get the roles!", 'thumbnail': '{server}', 'roles': []},
                    'channel': None,
                },
                'embedded_message': {
                    'sent': True,
                    'active': False,
                    'message': {'title': "Embedded Message", 'content': "This is an embedded message", 'thumbnail': '{server}'},
                    'channel': None
                },
                'auto_mod': {
                    'active': False
                },
                'level_rewards': {
                    'active': False,
                },
                'scheduler': {
                    'active': False,
                    'message': {'title': "Reminder", 'content': "Reminder for {user_mention}!", 'thumbnail': '{user}'},
                    'channel': None
                },
                'music_playback': {
                    'active': True,
                    'ban_roles': []
                },
                'download_audio': {
                    'active': True,
                    'ban_roles': []
                },
                'download_video': {
                    'active': True,
                    'ban_roles': []
                },
                'quote':{
                    'active': False,
                    'channel': None,
                    'can_quote': []
                },
                'private_vc': {
                    'active': False,
                    'ban_roles': []
                }
            },
            "channels": channels,
            "roles": roles
        }


    