#!/bin/sh
# Used only by SSH Deck.
# 1. Key setup: the password comes from a one-time named pipe in a private
#    runtime folder (path in SSHDECK_PW_FIFO). Nothing is written to disk.
# 2. Unlocking a key that has a passphrase: ask the user in a dialog.
if [ -n "${SSHDECK_PW_FIFO:-}" ]; then
    # Exactly one line, shell builtins only (nothing shows in the process list).
    IFS= read -r pw < "$SSHDECK_PW_FIFO" || exit 1
    printf '%s\n' "$pw"
    exit 0
fi
prompt="${1:-Passphrase for the SSH Deck key}"
if command -v kdialog >/dev/null 2>&1; then
    exec kdialog --title "SSH Deck" --password "$prompt"
fi
if command -v zenity >/dev/null 2>&1; then
    exec zenity --password --title="SSH Deck"
fi
echo "SSH Deck: no dialog program (kdialog or zenity) found" >&2
exit 1
