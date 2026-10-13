# ADR 0011: Scheduling math and calendar providers in the engine, interviews in Hire

Status: accepted

## Context

Brief 2.2 item 12 lists scheduling as an engine module: calendar integrations,
availability, time zones, buffers, meeting links, reminders, reschedule and cancel flows,
ICS files. Brief 3.6 asks Pairly Hire for interviewer availability, candidate
self-scheduling, panels, meeting links, reminders and briefings (WP2.8).

Only Hire schedules today, but nothing about a calendar is about hiring: Pay could book
a call with a vendor, and any workflow that sends a meeting needs the same ICS file. We
also have no Google Cloud project yet (an ASK item), so the Google code must exist and be
tested without credentials, and development must work with no external account at all.

## Decision

1. **Pure scheduling code lives in the engine**, `pairly_core.scheduling`:
   * `availability`: working hours per weekday (several ranges a day), buffers before and
     after, a daily cap, holidays, all in each person's own time zone through `zoneinfo`,
     per local date, so a slot is right on both sides of a daylight saving change.
     `find_slots` returns every slot where the whole panel is free; `conflicts` explains
     why one slot is not (shown inline when a recruiter drags an interview).
   * `ics`: RFC 5545 files written by hand (no library, so no license question): `UID`,
     `SEQUENCE`, `METHOD:REQUEST` or `CANCEL`, UTC times (no `VTIMEZONE` needed), text
     escaping and 75 octet folding.

2. **Provider interfaces live in the engine**, `pairly_core.integrations.calendar`:
   `CalendarProvider` (free/busy for a set of people, create, update and cancel events
   with attendees and an ICS UID, a meeting link when asked) and `MeetingProvider`.
   Implementations:
   * `FakeCalendarProvider`: deterministic busy blocks per person from a seed (SHA-256 of
     seed, person and local date), stored through a small store interface the first time
     a day is read; events written through it show as busy. Used in development, tests
     and the demo. Meeting links use the reserved `meet.pairly.example` name.
   * `GoogleCalendarProvider` and `GoogleOAuth`: free/busy, events with Meet through
     `conferenceData`, OAuth per person (offline access, narrow scopes). Tested against a
     mocked HTTP transport only. `sendUpdates=none`: Pairly sends its own invitations.
   * `MicrosoftGraphCalendarProvider` and `MicrosoftOAuth` (M3): free/busy through
     `getSchedule`, events through `/me/events` with attendees, `transactionId` (a
     retried create returns the same event) and Teams through `isOnlineMeeting` with
     `onlineMeetingProvider: teamsForBusiness`; OAuth per person like Google, refresh
     token encrypted. Graph assigns its own `iCalUId` (read only): it comes back on
     `EventRef.ical_uid`, and our ICS UID is kept as an extended property. Graph has no
     `sendUpdates=none`, so Exchange also sends its own invitation for the organiser's
     event (ASK: keep attendees on the Outlook event, or only the panel). Tested against
     a mocked transport only. Off by default: `hire.scheduling.microsoft` plus
     `PAIRLY_MICROSOFT_CLIENT_ID`, `PAIRLY_MICROSOFT_CLIENT_SECRET`,
     `PAIRLY_MICROSOFT_REDIRECT_URL` (and `PAIRLY_MICROSOFT_TENANT`, by default
     `organizations`).
   * `ZoomMeetingProvider` (M3): Server-to-Server OAuth with the organisation's own
     Zoom app (account id, client id, client secret, optional host user), meetings
     created, moved and deleted with the interview. A meeting-link provider only: the
     organiser's calendar provider writes the event with the Zoom link as its
     location. The app is saved by an administrator, checked with Zoom first, and
     stored encrypted in `hire_meeting_integrations` (migration `hire_0019`). Off by
     default: `hire.scheduling.zoom`. Tested against a mocked transport only.

3. **Email attachments** in the notification engine: `Extras.attachments` carries text
   attachments (calendar files) through the encrypted notification content to SMTP,
   Resend and the development outbox, as `text/calendar; method=...`.

4. **State stays in the workflow.** Hire owns its tables (migration `hire_0005`):
   interviews and panels, interviewer rules, the fake calendar's stored blocks, and the
   per person calendar connections with encrypted refresh tokens. The engine has no
   tables for scheduling. When a second workflow needs calendar connections, the
   connection table moves to the engine with a core migration; the provider code does
   not change.

5. **Google is off by default**: the `hire.scheduling.google` feature flag and the
   deployment's OAuth client (`PAIRLY_GOOGLE_CLIENT_ID`, `PAIRLY_GOOGLE_CLIENT_SECRET`,
   `PAIRLY_GOOGLE_REDIRECT_URL`) must both be set. Until then every person uses the fake
   calendar and the settings page says "not connected" with the steps. No external
   account is created by the code or the docs (brief rule 10).

## Consequences

* Any workflow can find a time for a group of people and send a correct calendar file
  without new engine code.
* Availability is computed in Python from free/busy data at request time; a window is at
  most 62 days, and a panel of five over two weeks takes milliseconds.
* Double booking is prevented in Hire, not in the provider: a booking takes a
  transaction-scoped advisory lock per panelist (in a stable order) and checks again
  under the lock, so two bookings that share a person are serialised.
* External writes cannot roll back with the booking, so each one is journalled
  (`hire_calendar_operations`, migration `hire_0020`, `pairly_hire.scheduling.operations`):
  a `pending` row is committed on its own before the provider call (provider, our
  idempotency key, the provider's id once known, the intended state), and the booking's
  own transaction marks it `done`, so the mark commits exactly when the change does. The
  `hire_calendar_reconcile` job (every 5 minutes) settles rows left pending: an orphan
  event or Zoom meeting is cancelled, a lost move or cancel is written again from the
  committed state, an unknown result is marked `failed` for a person to check. Audited.
* Real Google use needs the Google Cloud project (an open ASK item), an OAuth consent
  screen and a callback URL; then each interviewer connects their own calendar.
