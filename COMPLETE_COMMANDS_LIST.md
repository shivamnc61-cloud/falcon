# Falcon Premium Bot - Complete Commands List

**Total Commands:** 600+ registered prefix commands (core commands + Power Command shortcuts)  
**Last Updated:** October 8, 2026

Prefix command user/member arguments accept a Discord mention, a raw ID, `@ID`, or an exact username/display name in the current server. Reply to a member's message and omit the target to use the replied-to member. Role arguments accept a role mention, a raw ID, or `@ID`; slash commands continue to use Discord's native member and role selectors.

---

## 📑 Quick Navigation

- [Invite Commands](#invite-commands) (8)
- [Message Commands](#message-commands) (13)
- [Voice Commands](#voice-commands) (5)
- [VoiceMaster](#voicemaster) (28)
- [Social GIF Actions](#social-gif-actions)
- [GIPHY Commands](#giphy-commands)
- [Tickets & Reaction Roles](#tickets--reaction-roles) (26)
- [Birthdays & Live Stats](#birthdays--live-stats) (7)
- [Server Backups](#server-backups) (4)
- [Reminder Commands](#reminder-commands) (3)
- [Leaderboard Commands](#leaderboard-commands) (8)
- [Auto-Role Commands](#auto-role-commands) (4)
- [Giveaway Commands](#giveaway-commands) (8)
- [Timer Commands](#timer-commands) (4)
- [Greeting Commands](#greeting-commands) (4)
- [Moderation Commands](#moderation-commands) (10)
- [Guild Event Logs](#guild-event-logs)
- [Poll Commands](#poll-commands) (2)
- [Utility Commands](#utility-commands) (5)
- [Premium Commands](#premium-commands) (3)
- [Admin Commands](#admin-commands) (12)
- [Help & Info Commands](#help--info-commands) (4)

---

## 👥 Invite Commands

Track invitations and who invited members.

| Command | Alias | Parameters | Permission | Description |
|---------|-------|-----------|-----------|-------------|
| `!invites` | `!i` | `[user]` | Anyone | View valid joins, leaves, fake accounts, rejoins, and bonus invites |
| `!inviter` | - | `[user]` | Anyone | See who invited a user |
| `!invited` | - | `[user]` | Anyone | View members invited by a user |
| `!inviteinfo` | - | `[user]` | Anyone | Detailed invite information for a user |
| `!addinvites` | - | `<user> <amount>` | Administrator | Add bonus invites to a user |
| `!removeinvites` | - | `<user> <amount>` | Administrator | Remove invites from a user |
| `!clearinvites` | - | `[user]` | Administrator | Clear all invites (for all users if not specified) |
| `!resetmyinvites` | - | None | Anyone | Reset your own invite stats |
| `!resetinvites` | - | `<user> [all|joins|votes|leaves|fakes|rejoins]` | Administrator | Reset selected invite counters |

### Invite Configuration Commands

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!setinviterole` | `<role> <invites>` | Manage Guild | Set auto-role at invite milestone |
| `!unsetinviterole` | `<role>` | Manage Guild | Remove invite role requirement |
| `!viewinviteroles` | None | Anyone | List all invite role requirements |
| `!setaltthreshold` | `<days>` | Manage Guild | Mark accounts younger than this age as fake invites (`0` disables age checks) |
| `!unsetaltthreshold` | None | Manage Guild | Remove account age threshold |

---

## 💬 Message Commands

Track message counts across the guild.

| Command | Alias | Parameters | Permission | Description |
|---------|-------|-----------|-----------|-------------|
| `!messages` | `!m` | `[user]` | Anyone | View message stats for a user (self if not specified) |
| `!addmessages` | - | `<user> <amount>` | Administrator | Add messages to a user |
| `!removemessages` | - | `<user> <amount>` | Administrator | Remove messages from a user |
| `!clearmsgs` | - | `[user]` | Administrator | Clear all messages (for all users if not specified) |
| `!resetmymessages` | - | None | Anyone | Reset your own message stats |
| `!setmessagerole` | - | `<role> <count>` | Manage Guild | Set auto-role at message milestone |
| `!unsetmessagerole` | - | `<role>` | Manage Guild | Remove message role requirement |
| `!viewmessageroles` | - | None | Anyone | List all message role requirements |

### Blacklist Commands (exclude channels from tracking)

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!blacklistchannel` | `[channel]` | Manage Guild | Exclude channel from message tracking |
| `!unblacklistchannel` | `[channel]` | Manage Guild | Include channel in message tracking |
| `!blacklistedchannels` | None | Anyone | View all blacklisted channels |
| `!blacklistcategory` | `<category_name>` | Manage Guild | Exclude entire category from tracking |
| `!unblacklistcategory` | `<category_name>` | Manage Guild | Include category in tracking |
| `!blacklistedcategories` | None | Anyone | View all blacklisted categories |

### Message Configuration

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!setresettime` | `<mode> <time>` | Manage Guild | Set weekly/daily message reset time |
| `!variables` | None | Anyone | Show message variable placeholders |
| `!testmsg` | `<message>` | Manage Guild | Test join/leave message variables |

---

## 🎙️ Voice Commands

Track voice channel time.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!vc` | - | `[user]` | Anyone | View voice time stats for a user (self if not specified) |
| `!addvctime` | - | `<user> <seconds>` | Administrator | Add voice time to a user |
| `!reducevctime` | - | `<user> <seconds>` | Administrator | Remove voice time from a user |
| `!clearvoice` | - | `[user]` | Administrator | Clear all voice time (for all users if not specified) |
| `!resetmyvoice` | - | None | Anyone | Reset your own voice stats |

## VoiceMaster

Join the configured hub to create an open temporary voice room with an emoji-prefixed name. Empty managed rooms are deleted automatically. Room owners can lock rooms and manage individual member access.

| Command | Parameters | Permission | Description |
|---------|-----------|------------|-------------|
| `,voicehub` | `setup [hub] [panel] [category]` | Manage Guild | Create or reuse the hub and category, create a dedicated interface channel, and post one persistent golden control card |
| `,voicehub` | `status` / `disable` | Manage Guild | View or disable the hub (existing rooms remain managed) |
| `,room` / `,temp_vc` / `,temp` / `,tv` / `,temp_voice` | None | Anyone | Show the temporary voice-room command summary |
| `,room info` | None | Anyone | Show admin setup and owner command reference |
| `,room set_channel` | `<voice-channel>` | Manage Guild | Set the join-to-create channel |
| `,room set_category` | `[category]` | Manage Guild | Set or clear the category for new rooms |
| `,room rename` | `<name>` | Room owner | Rename your room with an emoji prefix |
| `,room limit` | `<0-99>` | Room owner | Set the user limit; zero means unlimited |
| `,room bitrate` | `<kbps>` | Room owner | Set room bitrate within the server's boost-tier limit |
| `,room region` | `[region]` | Room owner | List or set the RTC region (`auto` resets it) |
| `,room lock` / `,room unlock` | None | Room owner | Lock or unlock your room |
| `,room hide` / `,room unhide` | None | Room owner | Hide or show your room to other members |
| `,room allow` / `,room block` / `,room ban` | `<member>` | Room owner | Persistently allow or deny a member |
| `,room clear` | `[member]` | Room owner | Clear one member's overrides or all overrides |
| `,room permit` | `<member>` | Room owner | Allow a member to join and chat |
| `,room openchat` / `,room closechat` | None | Room owner | Open or close text-in-voice chat |
| `,room kick` / `,room mute` / `,room unmute` | `<member>` | Room owner | Disconnect or mute/unmute a room member |
| `,room summon` | `<member>` | Room owner | Move a member into your room |
| `,room claim` | None | Member in room | Claim a room after its owner leaves |
| `,room transfer` | `<member>` | Room owner | Transfer ownership to a current room member |
| `,room status` / `,room reset` | None | Room owner | View room state or reset settings and overrides |

---

## ⭐ XP & Leveling Commands

Leveling is disabled by default and must be enabled per server with `,leveling true`.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `,rank` | `[member]` | Anyone | Show a generated XP rank card with level progress and server position |
| `,levelprofile` | `[member]` | Anyone | Show the rank card, leveling profile, and daily streak |
| `,xpleaderboard` | `[limit 1-25]` | Anyone | View the server XP leaderboard |
| `,streak` | `[member]` | Anyone | View a daily-claim streak |
| `,claimreward` | None | Anyone | Claim daily XP and extend a consecutive-day streak |
| `,rewards` | None | Anyone | List level-based role rewards |
| `,serverstats` | None | Anyone | View server leveling statistics |
| `,leveling` | `[enabled] [reward_level] [reward_role]` | Manage Guild | Enable/disable XP and configure a level role reward |
| `,multiplier` | `<0.1-10>` | Manage Guild | Set the message XP multiplier |
| `,xpblacklist` | `add|remove @member` or `list` | Manage Guild | Exclude or restore a member's XP earning |
| `,voicexp` | `<amount> [interval_minutes]` | Manage Guild | Configure eligible voice XP |
| `,xpconfig` | `[daily_reward]` | Manage Guild | View settings or update the daily base reward |
| `,xpcooldown` | `<seconds>` | Manage Guild | Set message XP cooldown |
| `,xprange` | `<minimum> <maximum>` | Manage Guild | Set randomized message XP range |
| `,givexp` | `<member> <amount>` | Manage Guild | Grant XP to a member |
| `,setlevel` | `<member> <level>` | Manage Guild | Set a member's level |
| `,resetxp` | `<member>` | Manage Guild | Reset a member's XP, streak, and claim history |
| `,levelbackup` / `,backup` | `[guild_id]` | Bot Owner | DM a JSON export of leveling settings, rewards, blacklist, XP, and streak data |

Message XP, voice XP, eligible voice time, streaks, and reward roles are stored per server. Rank and profile commands render a custom card with the member avatar, level, total XP, progress bar, server position, and recorded voice time; profile cards also show the streak and voice activity. Voice XP and voice-time tracking exclude bots, AFK-channel users, and deafened users. Message XP has a per-member cooldown and duplicate-event protection.

---

## 🏆 Leaderboard Commands

View and manage leaderboards.

| Command | Subcommand | Parameters | Description |
|---------|-----------|-----------|-------------|
| `!leaderboard` / `!lb` | (main) | None | Main leaderboard view (invites) |
| `!lb` | `invites` / `!lb_invites` | None | View invite leaderboard |
| `!lb` | `messages` / `!lb_messages` | None | View total message leaderboard |
| `!lb` | `weekly` / `!lb_weekly` | None | View weekly message leaderboard |
| `!lb` | `daily` / `!lb_daily` | None | View daily message leaderboard |
| `!lb` | `voice` / `!lb_voice` | None | View total voice leaderboard |
| `!lb` | `weeklyvoice` / `!lb_weeklyvoice` | None | View weekly voice leaderboard |
| `!lb` | `dailyvoice` / `!lb_dailyvoice` | None | View daily voice leaderboard |

### Live Leaderboard Commands

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!createlivelb` | `[lb_type]` | Manage Guild | Create auto-updating leaderboard |
| `!deletelivelb` | `[lb_type]` | Manage Guild | Delete live leaderboard |
| `!showlivelbs` | None | Anyone | View all active live leaderboards |

---

## 🎭 Auto-Role Commands

Automatically assign roles on events.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!autorole` | None | Manage Guild | Show auto-role configuration menu |
| `,autorole add` | `[human\|bot] <role>` | Manage Guild | Assign to humans or bots; omitting the target applies to everyone. `,autorole add @role human` remains supported |
| `,autorole remove` | `<role>` | Manage Guild | Remove a configured join role |
| `,autorole list` | None | Manage Guild | List configured join roles |
| `!setautorole` | `<role> [type]` | Manage Guild | Add auto-role (type: all/bots/humans) |
| `!unsetautorole` | `<role>` | Manage Guild | Remove auto-role |
| `!viewautoroles` | None | Anyone | List all auto-roles |
| `,voicerole add` | `<role>` | Manage Guild | Grant a role when a member joins voice and remove it when they leave |
| `,voicerole remove` | `<role>` | Manage Guild | Remove a configured voice role; existing bot-granted roles are cleaned up on voice leave |
| `,voicerole list` | None | Manage Guild | List configured voice roles |

---

## Role Alias Commands

Create a toggle command with `,setup create <name> <role>`, then use `,<name> @user` to add or remove that role. The `,staff` name is available for a setup alias; use `,botstaff` or `,stafflist` to list bot staff. If another alias name is already used by a built-in command, use `,setup toggle <name> @user` instead. Use `,setup list` to view aliases and `,setup remove <name>` to delete one. `,setup reqrole <role>` restricts alias use to members with that role; server administrators bypass the requirement. Setup and role-result embeds do not notify mentioned users or roles.

---

## 🎉 Giveaway Commands

Create and manage giveaways.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!gcreate` | `<time> <winners> <prize>` | Manager+ | Create giveaway (react with 🎉 to enter) |
| `!gstart` | `<time> <winners> <prize>` | Manager+ | Start giveaway (alias for gcreate) |
| `!gwstart` | `<time> <winners> <prize>` | Manager+ | Start winner-role giveaway |
| `!greroll` | None | Manager+ | Reroll most recent giveaway |
| `!gend` | None | Manager+ | End active giveaway |
| `!setwin` | `<user>` | Manager+ | Force-set giveaway winner |
| `!finish` | None | Manager+ | Finish and announce giveaway winner |
| `!reroll` | None | Manager+ | Reroll last giveaway |

### Giveaway Configuration

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!setgiveaway` | None | Manager+ | Set giveaway winner role |
| `!unsetgiveaway` | None | Manager+ | Unset giveaway winner role |
| `!viewwinnerrole` | None | Anyone | View giveaway winner role |
| `!viewbypassrole` | None | Anyone | View bypass role (exclude from giveaway) |

---

## ⏱️ Timer Commands

Create countdowns and timers. Active and paused timers are stored and restored after a bot restart.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!tstart` | `<duration> [label]` | Anyone | Start a timer (e.g., "5m", "2h") |
| `!tpause` | None | Anyone | Pause active timer |
| `!tresume` | None | Anyone | Resume paused timer |
| `!tend` | None | Anyone | End active timer |

**Duration Format:** `10s`, `5m`, `1h`, `2d`

---

## 👋 Greeting Commands

Welcome messages use `/set greet message`, `/set greet channel`, and `/test greet`. Boost messages use `,set boost message <message>`, `,set boost channel <channel>`, and `,set boost test`. Add `{embed:name}` directly to a boost message, just as with greeting/event replies, to attach a saved embed. Create reusable embeds with `/embed create` or `,embed create`; add buttons in the editor and remove one by its button name.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
Configure each event with `,events set <event> <reply>` and `,events channel <event> <channel>`. Use `,events test <event>` to preview and `,events list` to inspect settings. Put `{embed:name}` in the reply to attach a reusable saved embed.

Use `,greet delete-after <join|leave|both> <seconds>` to auto-delete event greetings after a delay; use `0` to disable.

---

## 🧩 Community Utility Commands

Autoresponders and custom text commands are stored per server. Destructive changes require a confirmation button.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `,autoresponders add` | `<trigger> <response>` | Manage Guild | Add a case-insensitive keyword response |
| `,autoresponders edit` | `<trigger> <response>` | Manage Guild | Change a configured response |
| `,autoresponders options` | `<trigger> [mode] [channel] [role] [cooldown] [enabled] [clear_restrictions]` | Manage Guild | Configure contains/exact/starts/ends matching, restrictions, and cooldown |
| `,autoresponders test` | `<trigger>` | Manage Guild | Send a preview of the configured response |
| `,autoresponders list` | None | Anyone | View response matching and access settings |
| `,autoresponders remove` | `<trigger>` | Manage Guild | Remove a configured response after confirmation |
| `,customcommand add` | `<name> <response> [cooldown] [role] [channel]` | Manage Guild | Create a server-scoped command invoked with the bot prefix |
| `,customcommand test` | `<name> [args]` | Manage Guild | Preview a custom command response without deleting the test message |
| `,customcommand list` | None | Anyone | List command restrictions and cooldowns |
| `,customcommand remove` | `<name>` | Manage Guild | Remove a custom command after confirmation |
| `/embed create` / `,embed create` / `,embed edit` | `<name> [JSON config]` | Manage Guild | Open the editor for title, description, color, author, footer, images, and up to 18 live URL buttons; buttons can be removed by name and valid changes auto-save |
| `,embed show` / `,embed list` | `<name>` / None | Anyone | Send a saved embed or list saved embeds |
| `,embed send` | `<name> <#channel>` | Manage Guild | Send a saved embed to a selected channel |
| `,embeds remove` | `<name>` | Manage Guild | Delete an embed after confirmation |
| `,events set` / `,events channel` | `<join\|leave\|boost> <reply/channel>` | Manage Guild | Configure event reply templates and destinations |
| `,events test` / `,events list` | `<event>` | Manage Guild / Anyone | Test a reply or inspect event configuration |

Autoresponder and custom-command responses support existing user/server placeholders. Add `{delete}` to remove the triggering message, `{sendto:CHANNEL_ID}` (or a channel mention) to route the response, and `{embed:NAME}` to insert a saved embed. Custom commands also support `{args}` for text following the command name. Mentions are suppressed in generated responses.

---

## 🎭 Social GIF Actions

Use an action command with a member mention, member name, ID, or as a reply to their message. When `GIPHY_API_KEY` is configured, actions include G-rated GIPHY GIFs. Without the key or if GIPHY is unavailable, the text action still works without a GIF. Results rotate to avoid immediately repeating a GIF when alternatives are available. Use `spam` before the target to show the action five times in one message.

| Command | Example | Description |
|---------|---------|-------------|
| `,hug` | `,hug @user` / `,hug spam @user` | Hug a member, optionally as a five-line spam |
| `,slap` | `,slap @user` | Send a playful slap interaction |
| `,kiss` | `,kiss @user` | Send a kiss interaction |
| `,pat` | `,pat @user` | Send a head-pat interaction |
| `,highfive` | `,highfive @user` | High-five a member |
| `,wave` | `,wave @user` | Wave to a member |
| `,dance` | `,dance @user` | Dance with a member |

Other available actions include `cuddle`, `comfort`, `cheer`, `poke`, `tickle`, `blush`, `celebrate`, `welcome`, `fistbump`, `support`, `apologize`, and many more.

---

## GIPHY Commands

Set `GIPHY_API_KEY` to use these GIPHY GIF, sticker, category, and lookup endpoints. Results are restricted to a G rating. Search result buttons are requester-only.

| Command | Parameters | GIPHY endpoint | Description |
|---------|-----------|----------------|-------------|
| `,search` | `<query>` | GIF search | Browse matching GIFs |
| `,trending` | `[limit] [offset]` | Trending GIFs | Browse trending GIFs |
| `,random` | `[tag]` | Random GIF | Show a random GIF, optionally filtered by tag |
| `,translate` | `<phrase>` | GIF translate | Translate a phrase to a GIF |
| `,get` | `<id[,id...]>` | GIF by ID / GIFs by IDs | Retrieve one or multiple GIFs |
| `,categories` | None | GIF categories | List available categories |
| `,category` | `<category> [limit] [offset]` | Category tags | List a category's tags |
| `,category-tag` | `<category> <tag> [limit] [offset]` | Tagged GIFs | Browse GIFs for a category tag |
| `,sticker` | `<name>` | Sticker search | Browse matching animated stickers (`sticker-search` alias) |
| `,sticker-trending` | `[limit] [offset]` | Trending stickers | Browse trending animated stickers |
| `,sticker-random` | `[tag]` | Random sticker | Show a random animated sticker |
| `,sticker-translate` | `<phrase>` | Sticker translate | Translate a phrase to an animated sticker |

---

## 🎫 Tickets & Reaction Roles

Ticket panels are configured through the guided editor. `,ticket setup` opens the builder; admins choose a panel on page 1 and configure its ticket categories on page 2. The selected panel and its categories carry through later steps without repeated selectors. Category and channel dropdowns use Discord's built-in name search. The named-panel dropdown has a **Search panel names...** option that opens a text prompt and filters the panel list without a separate search button. On the options step, **Add option** opens a text-only modal for the option name, optional description and emoji, and menu placeholder. Each option automatically uses the next unused page-2 category, so each category is assigned only once and no category dropdown is shown on that step. If no categories are configured, **Add option** directs admins to page 2. Button panels accept a label and emoji. Dropdown options are saved immediately and update a published panel. The panel type publishes either ticket buttons or a ticket dropdown, never both. The embed step edits basic information, author, footer/timestamp, and images; it does not expose link-button creation/removal or custom-field creation controls. Existing embed content is retained. The editor also configures the transcript channel. Publish failures explain the missing channel, category, or permission directly in the setup panel. `,ticket edit` reopens the editor to rename, update, or republish panels. `,ticket delete` opens the editor's panel deletion controls. Panel changes are saved as they are made and published controls persist across restarts. Closing a ticket exports an HTML transcript to its panel's configured transcript channel, attempts to DM a copy to the opener, and moves the ticket to the optional close category.

| Command | Parameters | Permission | Description |
|---------|-----------|------------|-------------|
| `,ticket setup` / `/ticket setup` | None | Manage Channels | Open the guided builder for multiple ticket panels |
| `,ticket edit` / `/ticket edit` | None | Manage Channels | Rename, edit, or republish saved ticket panels |
| `,ticket delete` / `/ticket delete` | None | Manage Channels | Open the guided panel deletion controls |
| `,close` / `/close` | None | Ticket owner, support role, or Manage Channels | Export the transcript, lock, and close the current ticket |
| `,delete` / `/delete` | None | Support role or Manage Channels | Delete the current ticket after it has been closed |
| `,rename` / `/rename` | `<name>` | Ticket owner, support role, or Manage Channels | Rename the current open or closed ticket |
| `,ticket claim` / `/ticket claim` | None | Support role or Manage Channels | Claim an open ticket for staff follow-up |
| `,ticket transfer` / `/ticket transfer` | `<member>` | Support role or Manage Channels | Transfer an open ticket to another member and update access |
| `,ticket reopen` / `/ticket reopen` | None | Support role or Manage Channels | Reopen a closed ticket and restore owner access |
| `,ticket add` / `/ticket add`, `,ticket remove` / `/ticket remove` | `<member>` | Support role or Manage Channels | Add or remove a participant from the current ticket |
| `,ticket user_add` / `/ticket user_add`, `,ticket user_remove` / `/ticket user_remove` | `<member>` | Support role or Manage Channels | Ticket-group aliases for participant access changes |
| `,transcript` / `/transcript` | None | Support role or Manage Channels | Export the current ticket as HTML without closing it |
| `,ticketinfo` / `/ticketinfo` | None | Ticket owner, support role, or Manage Channels | View owner, status, claim, and open/close timestamps |
| `,ticket template_set` / `/ticket template_set` | `<name> <text>` | Support role or Manage Channels | Create or update a reusable ticket response |
| `,ticket template_list` / `/ticket template_list` | None | Support role or Manage Channels | List saved ticket templates |
| `,ticket template_delete` / `/ticket template_delete` | `<name>` | Support role or Manage Channels | Delete a saved template |
| `,ticket template_post` / `/ticket template_post` | `<name>` | Support role or Manage Channels | Post a template in an open ticket |
| `,reactionrole add` | `<message_id> <emoji> <role>` | Manage Roles | Map a message reaction to a role |
| `,reactionrole list` | None | Manage Roles | List reaction-role mappings |
| `,reactionrole remove` | `<message_id> <emoji>` | Manage Roles | Remove a reaction-role mapping |

Ticket channels include Claim, Close, Reopen, and Delete controls. Slash and prefix commands are available where shown; button colors are limited to Discord's native blue, grey, green, and red styles.

## 🎂 Birthdays & Live Stats

| Command | Parameters | Permission | Description |
|---------|-----------|------------|-------------|
| `,birthday setup` | `[#channel] [@role]` | Manage Guild | Set daily announcements; omit the channel to disable |
| `,birthday set` | `<month> <day>` | Anyone | Set your birthday in this server |
| `,birthday remove` | None | Anyone | Remove your birthday |
| `,birthday list` | None | Anyone | List birthdays registered in the server |
| `,birthday check` | None | Manage Guild | Manually announce today's registered birthdays |
| `,livestats setup` | None | Manage Channels | Create or refresh live member, human, bot, and boost voice stats |
| `,livestats remove` | None | Manage Channels | Delete configured live-stat voice channels |

The optional birthday role is assigned to birthday members for 24 hours, then removed automatically.

## 💤 Away Status

| Command | Parameters | Permission | Description |
|---------|-----------|------------|-------------|
| `,afk` | `[reason]` | Anyone | Set AFK status and temporarily prefix your nickname when Falcon can manage it |

Your AFK status clears when you send a message. Falcon restores your previous nickname if your nickname still has the `[AFK]` prefix; moderator changes made while you were away are preserved.

## 👋 Join Filter

| Command | Parameters | Permission | Description |
|---------|-----------|------------|-------------|
| `,joinfilter_setup` | `<enabled> [age_days] [no_avatar]` | Manage Messages | Configure account-age and profile-avatar join requirements |

Members who fail an enabled filter are sent a DM with the applicable reason before being kicked. If DMs are unavailable, Falcon logs that result and still enforces the configured filter.

## 💾 Server Backups

| Command | Parameters | Permission | Description |
|---------|-----------|------------|-------------|
| `,backup_create` | `<name>` | Administrator | Save roles, categories, and channel structure to Falcon's database |
| `,backup_list` | None | Manage Guild | List saved structure backups |
| `,backup_load` | `<name>` | Administrator | Preview and confirm a non-destructive restore of missing structure |
| `,backup_restore` | `<JSON attachment>` | Administrator | Validate an uploaded backup and preview a non-destructive restore |
| `,serverbackup` | `channel #channel` / `enable` / `disable` / `status` / `now` | Manage Guild | Configure hourly structure backups or create one immediately |

Manual backups are stored in Falcon's database and sent via Discord DM as a JSON structure export. Scheduled JSON snapshots are delivered to the selected channel. Backups are never restored automatically. Manual restore only creates missing roles and channels after confirmation; it does not overwrite existing resources.

---

## 🛡️ Moderation Commands

Manage members and messages.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!kick` | `<member> [reason]` | Kick Members | Kick member from guild |
| `!ban` | `<member> [reason]` | Ban Members | Ban member from guild |
| `!unban` | `<mention/ID/username>` or reply to a user's message | Ban Members | Unban a user |
| `!mute` | `<member> [duration] [reason]` | Moderate Members | Mute member (default 10m) |
| `!unmute` | `<member>` | Moderate Members | Unmute member |
| `!erase` | `[amount]` | Manage Messages | Delete messages (default 10) |
| `,purge` | `[@member] <amount>` | Manage Messages | Delete recent channel messages, optionally from one member |
| `,purgeall` | None | Manage Messages | Delete all messages visible in the channel history |
| `,channel delete` | `[#channel]` | Owner or Administrator | Pick a channel from the dropdown, then confirm deletion; channel argument is optional |
| `,role add` / `,role remove` | `<member> <role>` | Manage Roles | Add or remove one role |
| `,role all bot` / `,role all humans` | `<role>` | Manage Roles | Assign a role to all bots or all human members |
| `,roleall` | `<role>` | Manage Roles | Add a role to every member |
| `,roleall human add` / `,roleall bot add` | `<role>` | Manage Roles | Add a role to humans or bots |
| `,roleall human remove` / `,roleall bot remove` | `<role>` | Manage Roles | Remove a role from humans or bots |
| `,role create` | `<alias> <role>` | Manage Roles | Create a persistent self-service role toggle command |
| `,warn` | `<member> [reason]` | Manage Messages | Record a member warning |
| `,warnings` | `[member]` | Manage Messages | View up to 20 active warnings |
| `,clearwarnings` | `<member>` | Manage Messages | Clear all warnings for a member |
| `,unwarn` | `<warning_id> [member]` | Manage Messages | Remove one warning |

### Command/Module Disabling

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!disable` | `<mode> <target>` | Administrator | Disable command/module in channel |
| `!enable` | `<mode> <target>` | Administrator | Enable command/module in channel |

**Modes:** `command` or `module`

AutoMod, Anti-Nuke, and their whitelist/security-owner command interfaces are no longer registered.

---

## ⏰ Reminder Commands

| Command | Parameters | Permission | Description |
|---------|-----------|------------|-------------|
| `,remindme` | `<duration> <reminder>` | Anyone | Schedule a personal reminder |
| `,reminders_list` | None | Anyone | List your active reminders in this server or DM |
| `,reminders_cancel` | `<reminder_id>` | Anyone | Cancel one of your reminders |

---

## 📊 Poll Commands

Create and manage polls.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!createpoll` | `<args>` | Anyone | Create poll (custom format) |
| `!epoll` | None | Anyone | End most recent poll |
| `,poll` / `/poll` | `<question> <option1> <option2> [private]` | Anyone | Create a two-option button poll; set `private` in slash commands to make it visible only to you |

---

## 📋 Guild Event Logs

Configure a per-server event log. Logging is off until a server manager enables it. Deleted/edited message text is included in the configured log destination.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `,eventlog enable` | `[text_channel]` | Manage Guild | Enable event logging, using the private security log channel if no channel is supplied |
| `,setup_logs enable` | `[text_channel]` | Manage Guild | Alias for `,eventlog enable` |
| `,eventlog channel` | `<text_channel>` | Manage Guild | Change the event-log destination and enable logging |
| `,eventlog status` | None | Manage Guild | Show event-logging state and destination |
| `,eventlog disable` | None | Manage Guild | Disable general event logging |

Events: member joins/leaves/kicks/bans/unbans/timeouts, message edits/deletions and bulk deletions, voice state changes, and channel/role create/update/delete. Moderator attribution for kicks, bans, unbans, and timeouts is included when the audit-log entry is available; Falcon needs View Audit Log permission for attribution.

---

---

## 🔧 Utility Commands

General bot utilities.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!ping` | None | Anyone | Check bot latency |
| `,tagreply` | `[member]` | Anyone | Ping a mention, `@username`, ID, or the author of a replied message |
| `,setprefix` | `<prefix>` | Manage Guild | Set a server-specific prefix (the default prefix continues to work) |
| `,deleteprefix` | None | Manage Guild | Remove the server-specific prefix |
| `,status` | None | Anyone | View bot servers, users, latency, uptime, memory, and Python version |
| `,stats` | None | Anyone | View bot servers, users, latency, uptime, memory, and Python version |
| `!serverinfo` | None | Anyone | View guild information |
| `!userinfo` | `[user]` | Anyone | View user details (self if not specified) |
| `!avatar` | `[user]` | Anyone | View user avatar (self if not specified) |
| `!crypto` | `[symbol]` | Anyone | Get crypto price (e.g., BTC, ETH) |

Channel access commands require Manage Channels and default to the current channel when no target is provided.

| Command | Parameters | Permission | Description |
|---------|-----------|------------|-------------|
| `,lock` | `[channel]` | Manage Channels | Stop sending in text channels or connecting to voice channels |
| `,unlock` | `[channel]` | Manage Channels | Restore inherited sending or connect permissions |
| `,hide` | `[channel]` | Manage Channels | Hide a channel from @everyone |
| `,unhide` | `[channel]` | Manage Channels | Restore inherited visibility |

---

## ⭐ Premium Commands

Manage premium status.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!premium` | `<action> [target]` | Owner | Manage premium (add, remove, check) |
| `!premium_add` | `[user] [days]` | Owner | Add premium to user (default 30 days) |
| `!premium_remove` | `[user]` | Owner | Remove premium from user |

### No-Prefix Feature

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!noprefix` | `<action> <user> [duration]` | Owner | Allow commands without prefix for user |

---

## 👨‍💼 Admin Commands

Bot owner and staff management.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!setowner` | `<user>` | Primary Owner | Add bot owner |
| `!unsetowner` | `<user>` | Primary Owner | Remove bot owner |
| `!owners` | None | Anyone | List all bot owners |
| `!setstaff` | `<user>` | Bot Owner | Add bot staff member |
| `!unsetstaff` | `<user>` | Bot Owner | Remove bot staff member |
| `!botstaff` / `!stafflist` | None | Anyone | List all bot staff (no message when the list is empty) |
| `!blacklist_user` | `<user> [reason]` | Bot Owner | Blacklist user from bot |
| `!customize` | `[action] [url]` | Bot Owner | Customize bot appearance |
| `!emoji` | `[arg]` | Bot Owner | Manage custom emojis |
| `!refresh_emojis` | None | Bot Owner | Refresh emoji cache |
| `!reset_emojis` | None | Bot Owner | Reset to default emojis |
| `!bonus` | `<user> <amount>` | Bot Owner | Quick add invites to user |

---

## ℹ️ Help & Info Commands

Get help and information about the bot.

| Command | Parameters | Permission | Description |
|---------|-----------|-----------|-------------|
| `!help` | None | Anyone | Open interactive help menu |
| `!credits` | None | Anyone | View bot credits and developers |
| `!about` | None | Anyone | About Falcon Premium Bot |

---

---

## 📋 Command Groups Summary

| Category | Commands | Total |
|----------|----------|-------|
| Invites | invites, i, inviter, invited, inviteinfo, addinvites, removeinvites, clearinvites, resetmyinvites | 9 |
| Messages | messages, m, addmessages, removemessages, clearmsgs, resetmymessages, blacklistchannel, unblacklistchannel, blacklistedchannels, blacklistcategory, unblacklistcategory, blacklistedcategories, setmessagerole, unsetmessagerole, viewmessageroles, setresettime, variables, testmsg | 18 |
| Voice | vc, addvctime, reducevctime, clearvoice, resetmyvoice | 5 |
| Leaderboards | leaderboard, lb, lb_invites, lb_messages, lb_weekly, lb_daily, lb_voice, lb_weeklyvoice, lb_dailyvoice, createlivelb, deletelivelb, showlivelbs | 12 |
| Auto-Roles | autorole, setautorole, unsetautorole, viewautoroles | 4 |
| Giveaways | gcreate, gstart, gwstart, greroll, gend, setwin, finish, reroll, setgiveaway, unsetgiveaway, viewwinnerrole, viewbypassrole | 12 |
| Timers | tstart, tpause, tresume, tend | 4 |
| Greetings | setjoinchannel, unsetwelcomechannel, setleave, unsetleavechannel, setjoinmessage, unsetjoinmessage, setleavemessage, unsetleavemessage, setvanitymessage, unsetvanitymessage, setgreet, disablegreet, greetchannels, greetvariables | 14 |
| Moderation | kick, ban, unban, mute, unmute, erase, warn, warnings, clearwarnings, unwarn, disable, enable | 12 |
| VoiceMaster | voicehub, room info, room set_channel, room set_category, room lock, room unlock, room hide, room unhide, room rename, room limit, room bitrate, room region, room allow, room block, room ban, room clear, room permit, room openchat, room closechat, room kick, room mute, room unmute, room summon, room claim, room transfer, room status, room reset | 28 |
| Tickets & Reaction Roles | ticket setup/edit/delete (guided panel builder), ticket legacy setup/category/limit commands, close, delete, rename, claim, transfer, reopen, add/remove, transcript, ticketinfo, ticket templates, reactionrole add/list/remove | 27 |
| Birthdays & Live Stats | birthday setup, birthday set, birthday remove, birthday list, birthday check, livestats setup, livestats remove | 7 |
| Server Backups | backup_create, backup_list, backup_load, serverbackup | 4 |
| Reminders | remindme, reminders_list, reminders_cancel | 3 |
| Polls | createpoll, epoll | 2 |
| Utility | ping, status, stats, serverinfo, userinfo, avatar, crypto, lock, unlock, hide, unhide | 11 |
| Premium | premium, premium_add, premium_remove, noprefix | 4 |
| Admin | setowner, unsetowner, owners, setstaff, unsetstaff, staff, blacklist_user, customize, emoji, refresh_emojis, reset_emojis, bonus | 12 |
| Help | help, credits, about | 3 |

**Total Commands: ~120+**

---

## 🔑 Command Prefixes

- Primary Prefix: `!`
- Secondary Prefix: `,`
- No Prefix Mode: Available to premium users (e.g., `invites` works without prefix)

---

## ⚙️ Permission Levels

- **Anyone:** No special permissions required
- **Manage Guild:** Requires manage_guild permission
- **Manage Messages:** Requires manage_messages permission
- **Moderate Members:** Requires moderate_members permission
- **Ban Members:** Requires ban_members permission
- **Kick Members:** Requires kick_members permission
- **Administrator:** Requires administrator permission
- **Bot Owner:** Owner account or in bot_owners DB table
- **Bot Staff:** Owner/Staff account or in bot_staff DB table
- **Primary Owner:** Hardcoded PRIMARY_OWNER_IDS (immutable)

---

## 💡 Tips & Tricks

1. **Invite Tracking:** Automatically tracks who invited whom when they join
2. **Auto-Roles:** Can assign roles based on invites, messages, or on all/bots/humans
3. **Live Leaderboards:** Updates every 30 seconds automatically
4. **Message Variables:** Use `{user}`, `{username}`, `{server}`, `{membercount}`, `{inviter}` in custom messages
5. **Giveaways:** React with 🎉 to enter, bot picks random winner
6. **Voice Tracking:** Separate counters for alltime, weekly, and daily voice time
7. **Blacklisting:** Channels/categories can be excluded from message tracking
8. **No-Prefix:** Premium users can use commands without prefix

---

**End of Commands List**

Generated: July 29, 2026


## Stability & Power Update
- `,setup create` / `,setup reqrole` / `,setup list` / `,setup staff` / `,setup vip` / `,setup friends` / `,setup remove`
- `,customize name|bio|avatar|banner|reset`
- `,lock all` / `,unlock all`
- `,hide all` / `,unhide all`
- `,commandcount`
- Global user No-Prefix remains active across every guild until expiry/removal.

## Reference Bot Feature Additions (Non-AI)

The following additions are prefix commands and store configuration in Falcon's SQLite database.

### Automation

- `,autopost add #channel <interval> <message>` / `list` / `remove <id>` - Schedule recurring messages; intervals use formats such as `2h` or `1d`.
- `,autobump set #channel [interval]` / `status` / `disable` - Send scheduled bump reminders. Discord bots cannot invoke another bot's slash command on its behalf.
- `,autoreact add <trigger> <emoji> [#channel]` / `list` / `remove <id>` - React to messages matching configured text.
- `,vanityroles add <keyword> @role` / `list` / `remove <id>` - Grant or remove a role when a member's custom status contains the keyword. Requires the Presence intent and Manage Roles permission.

### Utilities and Profiles

- `,convert length|weight <amount> <from> <to>` - Convert common length or weight units.
- `,convert encode|decode <base32|hex|rot13|binary> <text>` - Convert text encodings.
- `,calculate <expression>` / `,calc` - Evaluate basic arithmetic without executing Python code.
- `,wiki <query>` - Fetch a Wikipedia summary.
- `,github <query>` / `,youtube <query>` - Open repository or video search results.
- `,globalboard [messages|invites]` - View global rankings.
- `,export <members|roles|bans|messages>` - Export a CSV (Administrator); message export covers the current channel's recent history.
- `,todo add|list|remove` - Manage a personal todo list.
- `,profileedit bio|background|social|clear` and `,profilecard [member]` - Customize and view profile details.
- `,animal [cat|dog|fox]` / `,meme` - Fetch an animal fact/image or a wholesome meme.
- `,fakehack [member]` - Show a fictional roleplay result; no account or server is accessed.
