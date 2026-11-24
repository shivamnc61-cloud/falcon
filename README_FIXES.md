# Falcon Premium Bot v2 — Stability & Power Update

## Included fixes
- Giveaway code was left unchanged.
- Fixed the duplicated `on_member_join` handler that was preventing welcome/invite processing from reaching the variable replacement helper.
- No-Prefix remains a **global user setting**: once a user is granted No-Prefix, it works in every server where Falcon is present until expiry/removal.
- Bot-owner mentions receive the requested crown reaction.
- Added server setup role management: Staff, VIP, Friends, create/list/remove.
- Added server Extra Owner management. Only the bot owner or current Discord server owner can add/remove Extra Owners.
- Added server customization: name/nickname, bio, avatar URL, banner URL, reset. Server owner, Extra Owners, and bot owners can manage it.
- Global bot account avatar/banner remain bot-owner-only because Discord applies those changes to the bot account globally.
- Added `lock all`, `unlock all`, `hide all`, and `unhide all`.
- Added a large Power Command shortcut library, pushing the registered prefix command count above 600 while keeping existing commands intact.
- Added better command error reporting instead of silently swallowing every unexpected command error.
- Added `requirements.txt`.
- Extended per-server autoresponders with contains/exact/starts/ends matching, channel/role filters, per-user cooldowns, response previews, `{delete}`, `{sendto:...}`, and saved `{embed:...}` insertion.
- Added per-server custom prefix commands with role/channel restrictions, cooldowns, `{args}` substitution, response previews, and confirmed deletion.
- Destructive removal of autoresponders, custom commands, saved embeds, greetings, and boost settings now asks for confirmation.
- Added per-server text/voice XP, XP rank cards and leveling-profile displays, XP leaderboards, streaks and daily claims, role rewards, and administration controls. Rank cards and profiles now also track and show eligible per-server voice time. Existing profile and general leaderboard commands remain unchanged; use `,levelprofile` and `,xpleaderboard` for leveling.
- Added configurable event logging for member joins/leaves, kicks, bans/unbans, timeouts, message edits/deletions, voice state changes, and channel/role changes. Configure it with `,eventlog` (alias `,setup_logs`) `enable [#channel]`, `channel #channel`, `status`, and `disable`; managing event logs requires Manage Server.

## Important permissions
For full functionality, give the bot the permissions it actually needs for the features you enable: Manage Channels, Manage Roles, Manage Messages, Kick Members, Ban Members, Moderate Members, and Send Messages/Embed Links.

## Main new commands
- `,setup create <name> <role>` / `,setup list` / `,setup remove <name>` / `,setup toggle <name> <user>` / `,setup reqrole <role>`
- `,customize name <name>` / `,customize bio <text>` / `,customize avatar <url>` / `,customize banner <url>` / `,customize reset`
- `,lock all` / `,unlock all` / `,hide all` / `,unhide all`
- `,commandcount`

## Removed Security Automation
AutoMod, Anti-Nuke, Anti-Raid, and their related whitelist/security-owner commands and implementation have been removed.

## Denycord-inspired community features
- Persistent ticket panels create private support channels; closing a ticket posts an HTML transcript to the ticket log channel and attempts to DM a copy to the ticket owner. Ticket templates can be set, listed, posted, and deleted.
- Reaction-role mappings assign/remove roles from raw reaction events and enforce Falcon's role hierarchy.
- Birthday announcements and member birthdays are stored per server. Daily announcements are claimed persistently to avoid duplicate sends after restarts; the optional role is removed after 24 hours. Use `,birthday check` for a manual announcement.
- `,livestats setup` creates voice channels for member, human, bot, and boost counts; `,livestats remove` removes the configured channels.
- `,voicehub setup [hub] [panel] [category]` configures an open-by-default join-to-create room system with persistent controls and owner-only room commands; when no hub is supplied, Falcon creates the hub and category. Use `,room info` for the full command list, or `,room set_channel` / `,room set_category` for split configuration.
- `,backup_create <name>` saves a structure backup and attempts to DM a JSON export; `,backup_restore <JSON attachment>` previews a non-destructive restore before confirmation.
- `,serverbackup channel #channel` followed by `enable` schedules hourly structure backups; `now`, `status`, and `disable` manage the schedule. Snapshots are never restored automatically.
- `,reminders_list` and `,reminders_cancel <id>` manage your reminders. Each user can have up to five active reminders per server or DM. Moderators can use `,warnings`, `,clearwarnings`, and `,unwarn`.
- `,afk [reason]` adds an `[AFK]` nickname prefix when Falcon has Manage Nicknames and a higher role; the original nickname is restored when the member sends a message, unless their nickname was changed while AFK.
- `,joinfilter_setup <enabled> [age_days] [no_avatar]` can kick accounts that fail the configured age/avatar requirements and attempts to DM each affected member with the specific reason before removal. A member with closed DMs is still filtered; the DM failure is logged.
