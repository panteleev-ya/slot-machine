# slot-machine Chrome extension

One button: after you sign in to the visa site yourself, it sends the session to the Pi's
renewal page over the home Wi-Fi, and the Pi checks with it right away.

It reads exactly one cookie (`_yatri_session` on `ais.usvisa-info.com`), and only when you
click. It talks only to the Pi (`yarikos.local`, or the address you set). It never sees
your password, solves no captcha, and stores only the Pi's address and token.

## Install (once)

1. `make renew-token` on the Mac. It sets up the Pi's side and copies the token to the clipboard.
2. Chrome → `chrome://extensions` → turn on **Developer mode** (top right) → **Load unpacked** →
   pick this `extension/` folder.
3. Pin it (puzzle-piece icon → pin **slot-machine: renew visa session**).
4. Right-click its icon → **Options** → paste the token → **Save**. The address is already
   `http://yarikos.local:8080`.

Chrome may warn about developer-mode extensions on start-up. That's expected for an
extension that isn't from the Chrome Web Store.

## Use

When the "session ended" message arrives:

1. Sign in at <https://ais.usvisa-info.com/en-kz/niv/users/sign_in> (captcha and all).
2. Click the extension → **Send session to the Pi**. It answers with the earliest date.

Don't sign out of the visa site afterwards: that ends the Pi's session too. Just close the tab.

After `git pull` or edits here, click the reload arrow on the extension's card in
`chrome://extensions`. After `make renew-token` again (a new token), paste it into Options again.
