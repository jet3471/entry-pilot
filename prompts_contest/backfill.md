# Task: estimate minimum prize values (no browsing, no tools)
Below is a JSON list of online events with their stored prize text. For each one estimate the fields below from that text only. Do not open pages, do not use any tool, do not run cp commands.

Fields per item:
- prize_min_value: integer KRW value of the LEAST valuable prize that ONE winner receives. Unknown or no clear value → null. Never guess wildly.
- winners: integer number of winners if the text states it (sum the tiers; "all participants" → null), else null.
- all_rewarded: 1 if every participant gets a reward ("all participants", "everyone who enters", "first N come, first served" does NOT count), 0 if it is a raffle or draw, null if unclear.
- note: at most 12 words naming the basis, e.g. "coffee coupon, lowest tier".

Value rules:
- Coffee coupon (Americano, Starbucks, Twosome, Mega Coffee) ~ 4,500-5,000 KRW; use 4,500 unless a price is stated. Mobile gift card / voucher: its face value. Points: 1 point = 1 KRW.
- Several tiers: use the lowest tier's value. Tiers with unknown values: null.
- JP prizes in JPY: multiply by 9 (assumption, 1 JPY = 9 KRW). US prizes in USD: multiply by 1,400 (assumption, 1 USD = 1,400 KRW). Other currencies: null.
- Physical goods without a stated price: null.

Reply with ONLY a JSON array, one object per item, same ids, e.g.
[{"id": "E12", "prize_min_value": 4500, "winners": 30, "all_rewarded": 0, "note": "coffee coupon"}]
