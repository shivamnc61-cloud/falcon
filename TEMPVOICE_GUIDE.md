# TempVoice / अस्थायी वॉइस रूम

Falcon TempVoice में Join-to-Create वॉइस चैनल, persistent SQLite रिकॉर्ड, owner controls और restart के बाद काम करने वाला control panel शामिल है।

## स्थानीय सेटअप

1. Python और repository dependencies इंस्टॉल करें:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. `.env.example` को `.env` में कॉपी करके `DISCORD_TOKEN` सेट करें। Token को `config.yml`, source code या public repository में न रखें।
3. Discord Developer Portal में **Server Members Intent** और **Message Content Intent** चालू करें। Bot को guilds, members और voice states intents के साथ चलना चाहिए।
4. Bot को कम-से-कम इन permissions के साथ server में invite करें:
   - Manage Channels
   - Move Members
   - View Channels, Connect, Send Messages, Attach Files और Read Message History
   - जरूरत पड़ने पर Deafen Members और Create Invite
5. Bot चलाएं:

   ```bash
   python main.py
   ```

## Discord में सेटअप

`Manage Server` permission वाले admin से:

1. `/tempvoice setup` में generator voice channel, panel text channel और वैकल्पिक category चुनें।
2. दूसरे Join-to-Create channels के लिए `/tempvoice set-generator` चलाएं। हर generator के लिए अलग category `/tempvoice set-category` से चुनी जा सकती है।
3. Panel को स्थानांतरित या फिर से पोस्ट करने के लिए `/tempvoice set-panel` चलाएं।
4. Staff override के लिए `/tempvoice set-staff` में role चुनें; `enabled` को false करने पर role हटेगा।
5. `/tempvoice config` और `/tempvoice stats` से settings देखें। `/tempvoice enable` और `/tempvoice disable` नई rooms के निर्माण को नियंत्रित करते हैं।
6. `/tempvoice cleanup` stale database records और खाली managed rooms साफ करता है। `/tempvoice backup` JSON export देता है और `/tempvoice restore` उसी server के export को वापस लोड करता है।

Generator में join करने पर bot room बनाता है, सदस्य को उसमें move करता है और room में persistent control panel पोस्ट करता है। खाली room अपने-आप हट जाता है। कई generator channels अलग-अलग categories इस्तेमाल कर सकते हैं।

## Owner commands

अपनी managed room में जुड़कर `/voice` commands चलाएं:

- `/voice name`, `/voice limit`, `/voice lock`, `/voice unlock`
- `/voice hide`, `/voice unhide`, `/voice status`
- `/voice permit`, `/voice reject`, `/voice trust`, `/voice untrust`
- `/voice block`, `/voice unblock`, `/voice kick`
- `/voice mute`, `/voice unmute`, `/voice deafen`, `/voice undeafen`
- `/voice transfer`, `/voice claim`, `/voice invite`
- `/voice delete`, `/voice reset`, `/voice interface`
- `/voice settings bitrate`, `/voice settings region`, `/voice settings textchat`

Room panel एक black graphic card है जिसमें heading, सभी 29 custom emoji और उनके bold command names image में बने होते हैं; नीचे के सभी clickable buttons उसी card में पाँच-पाँच के rows में होते हैं। Emoji artwork Discord CDN से load होता है, इसलिए bot host को Discord CDN तक network access चाहिए। Card का accent black है और सारे buttons Discord के uniform secondary/dark style में हैं; Discord custom button colors support नहीं करता। Panel में pagination या dropdown नहीं है। सभी emoji IDs runtime के `emojis.json` manifest के `voice_buttons` section से आते हैं। यदि button emoji दूसरे server के हैं, तो bot को target server/channel में **Use External Emojis** permission चाहिए। Bot restart पर saved room panels नए layout में refresh होते हैं। Private confirmations ephemeral होते हैं। Owner checks हर interaction पर वर्तमान room और database ownership से दोबारा सत्यापित होते हैं; server admins और configured staff roles override कर सकते हैं, Discord role hierarchy और bot permissions फिर भी लागू रहते हैं।

Discord voice channels पर slowmode उपलब्ध नहीं है, इसलिए `/voice slowmode` उपलब्ध नहीं कराया गया है। `config.yml` में `auto_transfer_owner: true` होने पर owner के room छोड़ने के बाद सबसे लंबे समय से server में मौजूद non-bot सदस्य owner बनता है; बराबरी होने पर कम user ID चुनी जाती है। यह विकल्प default रूप से बंद है।

## कॉन्फ़िगरेशन और backup

Non-secret defaults `config.yml` के `tempvoice` section में हैं: room name, user limit, bitrate, RTC region, empty-room cleanup, claim policy, staff role IDs, enabled controls, creation cooldown, guild room cap, audit logging और embed theme। Discord token केवल `.env` में रखें।

TempVoice JSON backup केवल उसी guild में restore होता है जिसके ID से वह बना था। Restore से उस guild की TempVoice settings, generators, staff roles, room records और overrides replace होते हैं; उपलब्ध न रहने वाले temporary voice channels के stale room records छोड़ दिए जाते हैं।

## Railway और अन्य hosting

- Railway पर repository deploy करें; root `Dockerfile` खुद detect होगा और `python main.py` चलाएगा। Bot service में `DISCORD_TOKEN` दें और Railway PostgreSQL service से `DATABASE_URL` reference करें। Railway के लिए पूरा setup [README](./README.md#deploy-on-railway) में है।
- SQLite ही इस्तेमाल करना हो तो Railway volume mount करके `DATABASE_PATH` को mounted path, जैसे `/data/falcon_data.db`, पर सेट करें। Ephemeral filesystem पर database रखने से redeploy के बाद data खो सकता है।
- किसी hosting plan को 24/7 uptime वाला न मानें जब तक plan स्पष्ट रूप से process को लगातार चलाने की अनुमति न देता हो।
- Termux पर पहले Python और `requirements.txt` की सभी dependencies install करके जांचें; किसी dependency के लिए compatible wheel उपलब्ध न हो तो platform पर installation समर्थित नहीं हो सकती।

## परीक्षण

TempVoice-focused tests चलाने के लिए:

```bash
python -m pytest tests/bot_smoke_test.py -k 'voice or tempvoice' -q
```

पूरे bot के smoke tests चलाने के लिए:

```bash
python -m pytest tests/bot_smoke_test.py -q
```

### Feature checklist

- **Implemented:** multi-generator Join-to-Create, per-generator categories, automatic empty-room cleanup, persistent SQLite ownership and overrides, owner/staff controls, persistent room panel, slash and prefix commands, admin configuration, same-guild JSON backup/restore, and stale-room record cleanup after restart.
- **Implemented by design:** no `/voice slowmode` command is exposed because Discord voice channels do not support slowmode.
- **Tested locally:** the focused voice/TempVoice selection and the complete bot smoke-test file use mocked Discord interactions and APIs. Python compilation is also checked before packaging.
- **Not integration-tested:** live Discord permission and role hierarchy behavior, publishing slash commands in a real guild, Discord API rate limits, Railway persistent-volume mounting, and Termux dependency installation. Test these in a separate server before production use.

The repository contains the project source, tests, configuration template, and setup documentation. Keep `.env` and SQLite database files private, and configure a persistent database path for each deployment.
