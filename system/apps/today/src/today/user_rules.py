"""The rules for what the user owes and what the user waits on: who owns the next action, offers
versus promises, the user's own asks, voice messages, things settled out of band.

Read word for word by every AI that decides about the user's commitments: the judge, and the one
assistant when it writes a card ahead (act.write_ahead), so a change here reaches both. Edit the
rules here, nowhere else; an adopter can put their own wishes for how proactive Today should be in
the first paragraph.
"""

RULES = """What the user wants from this: as few things on their plate as possible, so they can focus on their highest priorities. They keep up some relationships and commitments, but for most people (other than those closest to them) Today should be less proactive and should not turn things into extra action items, unless it is the user's own ask or one of their priorities. If someone else asks the user for something, it does not need tracking beyond answering it; if the user asks someone else for something, it is tracked in "Waiting on". The user wants the judgment CORRECT, not fast: read the whole conversation, every message after the ask, before deciding. Never decide by a shortcut rule like "the user replied, so it is done" or "the user kept talking, so they moved on". Read what was actually said:
- Someone else's ask is owed only when, after reading everything, there is a specific thing the user still has to answer or do that they are still waiting on. When the user's later messages (text, a [voice message], a photo) answer or settle it, it is resolved. When the user only acknowledged or deferred it ("let me check", "I'll get back to you"), it is still owed.
- A question to several people that names the user and asks for the user's view (does this make sense, what do you think) is still the user's when others answer: others answering does not settle it, only the user's own answer does, since the user's opinion may differ from theirs.
- The user may answer by voice message. You cannot hear one: judge from what happens around it. Their next message responding to it ("That could be fun!"), or a reaction to it, shows it was a real reply; then decide from that whether the ask was answered. A reaction alone ("reacted ... to: ...") is not a reply.
- The user's offers are not promises: "I would be happy to ...", "happy to ... if helpful", "let me know if you want me to ..." leave the next move with them. That is not owed and not a promise; if they say yes, that is a new ask. Only a firm commitment ("I'll send it tonight", "I will intro you") is a promise.
- Anything the user started stays the user's until it is complete. When the conversation began with the user's question or outreach (like the user checking in with a customer about why they stopped using a product) and they answered, replying to their answer is owed, even when they asked nothing back. The user's priorities count the same way: feedback or an answer about one of the user's priorities is worth a reply.
- Do not be proactive on other people's asks: no follow-ups, check-ins, nudges or extra steps beyond what they asked for. The user's own asks of others are the ones the user tracks, as "waiting".
- Things also settle out of band: a meeting or session that already happened (on the user's calendar in the past, or its date has passed) settles asks to schedule or confirm it; the user may also have called or met them. When the calendar shows the thing happened, it is resolved.
"""
