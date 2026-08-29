# Shift times, by show type

Every position's clock times come from one of three places, checked in this order.
Source: `scheduling/services/engine.py`.

1. **Dwight's Wedding** — matched on the title containing "dwight"
2. **Regular house timing** — matched on the show's **name**, or failing that on a
   start time of exactly **18:30**
3. **Everything else** — derived from that show's own doors-open and wrap-up times

The regular-timing shows are named so they are recognised however the calendar records
the clock: **Home Sweet Home-i-cide**, **Forever Country…In The Key Of Spirit**, and
**Shift Happens** — all doors 6:30pm, dinner 7pm, curtain 8pm. Matching on start time
alone is fragile; Dwight's is the proof that the same production gets entered against
the doors on some dates and the curtain on others. A regular show that drifts off 18:30
would otherwise fall back to derived offsets and silently re-time the whole crew.

The first two are management's own call times: clock times a person is told to arrive
at, not offsets. "Server 2 comes in at four" is the instruction that actually gets
given, and deriving it from the curtain drifts whenever a show's recorded times move.

Dwight's is matched on **title, not start time**, because the calendar records some
Dwight's dates against the curtain (18:30) and some against the doors (18:00). The
title is the thing that reliably says which timetable applies.

---

## Ordinary evening — doors 6:30pm

| Position | In | Out |
|---|---|---|
| Server Manager | 14:00 | 21:00 |
| Lead Server | 15:00 | 21:00 |
| Server 2 | 16:00 | 21:30 |
| Bartender | 16:00 | 23:00 |
| On-call Bartender | 17:30 | 22:30 |
| Server 3 | 17:30 | 23:00 |
| On-call Server | 18:15 | 23:00 |
| **50/50** | **18:30** | **21:30** |
| Busser | 18:45 | 23:00 |

## Dwight's Wedding — doors 6:00pm, Act I 6:30, dinner 7:30

| Position | In | Out |
|---|---|---|
| Server Manager | 14:00 | 21:00 |
| Lead Server | 15:00 | 21:00 |
| Server 2 | 15:30 | 21:30 |
| Bartender | 16:00 | 22:30 |
| Server 3 | 17:00 | 22:30 |
| On-call Bartender | 17:00 | 22:30 |
| **50/50** | **18:00** | **21:30** |
| On-call Server | 19:00 | 22:30 |
| Busser | 19:15 | 23:00 |

50/50 sells from doors, so it starts when the doors do: 18:30 on an ordinary evening,
18:00 for Dwight's. Both finish at 21:30.

## Extra positions on a big night

Server 4–6 and Bartender 2–3 have no call times of their own. They take the last-in
position's, so one crew is never on two different clocks for the same show:

| Extra position | Takes the times of |
|---|---|
| Server 4, 5, 6 | Server 3 |
| On-call Server 2, 3 | On-call Server |
| Bartender 2, 3 | Bartender |
| On-call Bartender 2 | On-call Bartender |

---

## Anything else — derived from the show's own times

A matinee, a private booking, an odd one-off: the window is built from that show's
doors-open and wrap-up, offset per position.

| Position | Starts before doors | Ends after wrap |
|---|---|---|
| Bartender (all) | 60 min | 15 min |
| Lead Server | 45 min | 15 min |
| Server 2–7 | 30 min | 15 min |
| Busser | 30 min | 30 min |
| On-call Server / Bartender | 0 | 0 |
| *anything with no entry* | 30 min | 15 min |

Worked example — **Private @ Gower, doors 20:00, wrap 23:50**:

| Position | Window |
|---|---|
| Bartender | 19:00 – 00:05 |
| Lead Server | 19:15 – 00:05 |
| Server 2 / 3 | 19:30 – 00:05 |
| Busser | 19:30 – 00:20 |
| On-call Server / Bartender | 20:00 – 23:50 |

50/50 has no doors to follow on a show like this and falls back to a fixed
**18:00 – 21:30**.

---

## Two things to be aware of on derived shows

**The Server Manager has no offset entry**, so it falls to the default 30/15 and comes
out at 19:30 – 00:05 on the example above. On both named timetables the manager is in
at 14:00; on a derived show they arrive with the servers instead. If a private booking
should have a manager in from mid-afternoon, that needs an offset of its own.

**The 50/50 fallback can land outside the show.** On the 20:00 Private @ Gower it reads
18:00 – 21:30 — two hours before doors, finishing mid-show. A private booking probably
has no raffle at all, but if one runs, its times need setting for that show.

Both are consequences of these shows matching neither named timetable. Adding a third
table, or per-show call times, is the fix if they become common.

---

## Where availability meets this

Availability must **fully cover** the window, not overlap it. A person free
17:30 – 21:30 cannot take Server 3 on an ordinary evening, which runs to 23:00. This is
the single most common reason a person with hours on file still does not appear on a
schedule — see [why-someone-is-not-scheduled.md](why-someone-is-not-scheduled.md).
