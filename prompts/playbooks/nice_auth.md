# Playbook: phone SMS code / identity verification (NICE, PASS SMS, KCB, i-PIN, certificates)
An agent cannot receive an SMS code or pass an identity check, and never pays or registers a card.
## verify
- The entry needs an SMS code, a phone identity check, i-PIN, a certificate or payment → manual --need identity (payment: manual --need payment) with --reason naming the step.
## submit
- You reach such a step during the entry → stop at once: manual --need identity --reason "<step>". Never type or store the user's number, birth date or any code.
