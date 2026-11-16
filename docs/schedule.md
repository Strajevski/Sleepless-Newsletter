# Editing the weekly schedule

All operational timing for the current configuration lives in `config/schedule.json`. Edit that file as ordinary JSON: quoted keys/strings, no comments or trailing commas. `config/delivery.json` now contains delivery policy and review holds, not times. Editor/captioner model-call timeouts remain in their own configuration files.

## Settings

| Fields | Meaning |
| --- | --- |
| `timezone` | Display timezone used for submissions and newsletter dates. |
| `cutoff_weekday`, `cutoff_time`, `cutoff_timezone` | Weekly submission deadline. An arrival exactly at the deadline belongs to the next issue. |
| `retry_day_offset`, `retry_time`, `retry_timezone` | The one deferred editor retry for an issue. This must follow its cutoff and precede delivery. |
| `media_day_offset`, `media_time`, `media_timezone` | When image-size preparation becomes eligible. It also runs before sending if needed. |
| `issue_send_day_offset`, `issue_send_time`, `issue_send_timezone` | When that issue is due for delivery. |
| `reminder_day_offset`, `reminder_time`, `reminder_timezone` | Reminder for that issue, sent to the whole roster. |
| `invitation_day_offset`, `invitation_time`, `invitation_timezone` | Invitation for that issue. It waits for complete delivery of the previous issue. |
| `catch_up_minutes` | Automatic sending window starting at each campaign's due time. Its end is exclusive. |
| `delivery_retry_minutes`, `delivery_retry_limit` | Minimum wait after a safe sending failure and number of additional automatic retries. Uncertain delivery still stops for review. |
| `intake_interval_minutes` | How frequently to scan the inbox, anchored to UTC epoch minutes. With 30, scans occur at :00 and :30. Configured cutoff, media, and editor retry times trigger extra scans even between those checks. |
| `first_issue_week`, `first_issue_number` | Issue numbering baseline. Keep these stable for an existing installation. |
| `version` | Configuration format; keep this at `2`. |

Clock times use 24-hour `HH:MM`. Timezones use IANA names, such as `UTC` or `Europe/London`. UTC keeps a fixed time all year; Europe/London follows the UK clock, including British Summer Time. Each event's timezone is explicit.

Day offsets are calendar days relative to **that issue's cutoff date**: `0` is the same date, `1` the next date, `-2` two dates earlier. The date is interpreted in the event's configured timezone. Using one timezone for the whole timeline is easiest to reason about.

The current Monday cutoff therefore gives:

| Event | Offset / weekday | Time (UTC) |
| --- | --- | --- |
| Invitation | -7: previous Monday | 08:00, after previous issue delivery |
| Reminder | -2: Saturday | 17:00 |
| Cutoff | Monday | 03:00 |
| Image preparation | 0: Monday | 04:00 |
| Editor retry if deferred | 0: Monday | 07:00 |
| Issue delivery | 0: Monday | 08:00 |

An invitation belongs to the **upcoming** issue. Its `-7` offset is why the next issue's invitation can follow this issue's delivery immediately. Give invitations a later time or date to separate them. If their nominal time precedes previous issue delivery, they wait for completion within their own sending window. A held, partial, or uncertain previous delivery prevents sending the invitation.

For a Thursday cutoff with Friday delivery and Saturday invitations, change `cutoff_weekday` to `Thursday`, `retry_day_offset` to `1`, `issue_send_day_offset` to `1`, and `invitation_day_offset` to `-5`. Keep `reminder_day_offset` at `-2` for Tuesday reminders. Set invitation time to `09:00` if desired; the other clock values can remain as above.

Issue IDs continue to name the Sunday at the start of the cutoff cycle, even when the cutoff moves to another weekday. Do not rename existing issue folders.

## Check before enabling sending

From the project folder, run:

```powershell
python scripts/schedule.py --issue 2026-W40
```

This validates the configuration and prints all event dates/times in UTC, without contacting email, Discord, or AI services. Use an upcoming issue ID to inspect your next cycle. Invalid clocks, timezones, missing fields, and impossible event ordering are rejected. A full year is checked for timezone transitions. A nonexistent local time during a daylight-saving jump is rejected; a repeated local time uses its first occurrence. UTC avoids both cases.

Timing edits are read on the next check. Change schedules between cycles, preferably while automation is stopped. Already frozen issues retain their input; completed drafts are not rebuilt. Changing a campaign's due time after its delivery journal has been created requires manual review: its original due time is part of the duplicate-send protection.

## Windows task upgrade (once)

The supplied task definitions now run a small check every minute, regardless of weekdays or daylight saving. The Final Build task calls `schedule.py --run-intake`; most checks exit immediately, while due checks run the existing intake pipeline. The Delivery task calls `send_due.py`, which applies the JSON timing and retry rules. Actual work starts on the next successful check, not necessarily at second zero. A missed stage check catches up on the next periodic intake run. Missed delivery windows still require manual review.

On the intended production machine, after copying and checking the release:

```powershell
.\scripts\install_final_build_task.ps1
.\scripts\install_delivery_task.ps1
Disable-ScheduledTask -TaskName 'Circus Newsletter Intake'
```

Disable the old regular Intake task **after** registering the replacement: the Final Build clock now supplies its periodic scans as well as the stage checks. Keep its registration for existing manual-operation tools. This avoids a second independent scan schedule. The installers use their checkout folder and current Windows identity by default; optional `-Root`, `-Account`, and `-Python` parameters support a different installation. Task Scheduler prompts locally for the account password. Validate the schedule before registering tasks, because they enable unattended operation.

Once upgraded, editing schedule.json needs no task reinstallation. Update the registered credentials if the Windows account password changes. Older unversioned installations can still read their original delivery.json timings during migration; version 2 takes all timing values from schedule.json even if old keys remain there.
