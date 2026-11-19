# Channel Copy – Heroku

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME)
(upar wale link me `YOUR_GITHUB_USERNAME/YOUR_REPO_NAME` apne GitHub repo se badal do)

Old channel ke saare audio/video ko new channel me copy karta hai (server-side, download/upload nahi).

## 1. Session string banao (apne PC par, ek baar)
```
pip install pyrogram tgcrypto
python generate_session.py
```
API_ID / API_HASH https://my.telegram.org se. Output wali SESSION_STRING copy karo (ye account ki key hai, kisi ko mat do).

## 2. Heroku par deploy
```
git init && git add . && git commit -m "init"
heroku create apna-app-name
heroku config:set API_ID=... API_HASH=... SESSION_STRING=... \
                  OLD_CHANNEL=-100... NEW_CHANNEL=-100...
git push heroku HEAD:main
heroku ps:scale worker=1
heroku logs --tail
```
(Aap new channel me admin hone chahiye, aur dono channels aapke account ke chats me dikhne chahiye.)

## 3. Khatam hone par
Logs me "Sab copy ho gaya" aaye to: `heroku ps:scale worker=0`

## Khatam hone par notification
Kaam poora hone (ya error se ruk jane) par aapke Telegram **Saved Messages** me message aata hai.
Phone par asli notification chahiye to optional:
1. @BotFather se bot banao, token lo, aur us bot ko ek baar `/start` bhejo.
2. @userinfobot se apna user id lo.
3. `heroku config:set BOT_TOKEN=... NOTIFY_USER_ID=...`

## Notes
- Worker dyno hai, web nahi, isliye PORT ki zarurat nahi.
- Progress Telegram "Saved Messages" me `CHANNEL_COPY_PROGRESS ...` message me save hota hai. Usse delete mat karna. Dyno restart par wahin se aage badhta hai.
- Naye sire se shuru karna ho to wo message delete kar do.
- Optional config vars: `BATCH` (max 100), `DELAY` (seconds, default 4; kam mat karo).
- Agar channel par "Restrict saving content" ON hai to copy nahi hoga.
