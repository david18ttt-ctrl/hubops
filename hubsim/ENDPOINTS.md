# WMS simulator endpoints

`python3 hubsim/server.py` serves these on `http://127.0.0.1:8765` (stdlib only). Self-test: `python3 hubsim/selftest.py`.

- Every endpoint is `POST` with a JSON body and answers `{"code": 0, "msg": "ok", "data": ...}`.
- A request the WMS refuses (missing sorter id, range too long, bad body) answers `{"code": -1, "msg": "<reason>"}`.
- Auth: any `Authorization: Bearer <token>`; no header or the token `expired` gets HTTP 401 (except the dispatch table).
- `GET /api/health` shows the simulated clock, the night being served and the endpoint list.
- Paged lists take `pageNum` / `pageSize` and return `total`.
- Times come back as `YYYY-mm-dd HH:MM:SS` strings, plus milliseconds in the `opt…Time` / `…Timestamp` / `sortTime` / `opTime` fields. The truck scan list writes `createTime` month first (`11/25/2026 21:07:54`).

Ids: hub `HOU.H` is `1` in both the operations and the truck (TMS) calls; other hubs `PHX.H`…`SAN.H` are 11–18, stations `AUS01`…`SGR01` are 101–112 (`/api/places`). Sorter 1 is `sorterId: 1`.

| Path | Body | Returns (`data`) |
|---|---|---|
| `/api/places` | `{}` | `[{id, regionName, regionCode, type}]`: every hub and station |
| `/api/bags/search` | `{packageNoList: [...], pageNum, pageSize}` or `{packageNoList: [], createBeginTime, createEndTime, startCenterId: 1, pageNum, pageSize}` | `{list, total}` bags, newest first: `packageNo` (`BG01…-<chute>`), `bagType` (2 sorter, 1 manual, 3 forecast), `bagTypeString` (`Sorter` / `Manual` / `Customer Forecast`), `packageTypeString`, `startCenterId`, `startCenterName`, `destinCenterName` (`VCT01`, `PHX.H`), `destinAreaCode` (route `VCT01-004`), `waybillCount`, `packageNum`, `createTime`, `optCreateTime`, `optFinishStatusTime` (closed), `optCheckOutTime` (signed out), `createByName`, `loadStatusString`, `warehouseStatus`, `exchangeBureauCode`, `chuteNo`. A number search ignores the dates and `startCenterId`, and also finds inbound gaylords (`GL…`, `startCenterName` = their origin hub) |
| `/api/bags/parcels` | `{packageNo, deptId: 1, pageNum, pageSize}` | `{list, total}`: `waybillNo`, `packageNo`, `optLoadTime` (ms bagged), `weight`, `statusString` (newest bag with that number) |
| `/api/sorter/records` | `{noType: 1, sorterId: 1, noList?: [waybills ≤100], sortTimeStart: ms, sortTimeEnd: ms, sortStatus?: 1 sorted / 2 failed, pageNum, pageSize}` | `{list, total}` newest first: `id`, `orderCode`, `barcodes`, `sorterId`, `platId` (feed seat), `sortTime` (ms), `deviceSortTime`, `sortStatus`, `sortStatusDesc`, `matchedChutes` (`["049"]`, empty for no-read / no-rule), `dropChuteNo`, `chuteRemark` (route on the matched chute), `refluxReason` (code), `refluxReasonName`. Without `sorterId`: code -1. Range over 31 days: code -1 |
| `/api/sorter/chute-log` | `{opTimeStart: ms, opTimeEnd: ms, chuteNo?, pageNum, pageSize}` | `{list, total}` newest first: `chuteNo`, `bagLabelCode` (the bag the print closed; empty on a failed print), `opType` (1 label print, 2 chute-full signal), `opTypeName`, `opResult` (1 ok, 0 failed), `opTime` (ms), `opTimeStr` |
| `/api/sorter/feed` | `{centerId, startTime, endTime, timeZone}` (a working-day window) | `[{hourRange: "YYYY-mm-dd HH", seatNo, seatName, pickCnt, refuseCnt}]`: one row per hour with `seatNo: null` (the whole sorter), then one per feed seat `"1"`…`"30"` |
| `/api/sorter/plan` | `{}` | the active sort plan: `{chutes: {chute: route}, dests: {route: [chutes]}, exceptions}` |
| `/api/trucks/tasks` | `{data: {destTransitIdList: [1]` (into the hub) **or** `originTransitIdList: [1]` (out of it), the other list optional to narrow to one place, `timeType: 1, startLocalTime, endLocalTime}, pageNum, pageSize}` | `{records, total}`: `taskNo` (`TT…`), `placeOfOrigin`, `destination`, `originTransitId`, `destTransitId`, `plannedDepartureTime`, `plannedArrivalTime`, `timeOfArrivalOrigin` (at the origin's dock), `actualDepartureTime`, `actualArrivalTime`, `estimationArrivalTime`, `loadBagCount`, `loadWaybillCount`, `unLoadBagCount`, `unLoadWaybillCount`, `modelName`, `vehicleStatusStr`, `licensePlateNo`, `transportTypeStr`, `estimationLoadingRate` (outbound, % of room). Outbound tasks appear 15 min before the truck docks |
| `/api/trucks/scans` | `{data: {taskNo, destinationTransitId?, originTransitId?, scanType, sealNo, serialNumber, startTime, endTime}, pageNum, pageSize}` | `{records, total}` newest first, one row per parcel per scan: `taskNo`, `waybillNo`, `bagNo`, `operationType` (`"1"` loading check-out at the origin, `"0"` unloading check-in at the destination), `operationTypeName`, `operatorDept`, `targetCenterName` (parcel's destination), `createByName`, `createTime` (month first). `taskNo` is required; range over 5 days: code -1 |
| `/api/scans/history` | `{operateNoType: "10"` waybill / `"20"` bag, `operateNos: [...], operateTimeStart, operateTimeEnd, pageNum, pageSize}` | `{list, total}` newest first: `operateNo`, `operateType` (100 order, 612 pickup arrival, 200 checkout, 410 loading, 411 departure, 412 arrival, 211 unload, 217 bagging, 202 check-in), `operateTypeDesc`, `operateTime`, `operateTimestamp`, `operatorDeptName`, `operatorName`, `bagNo`, `checkoutNo`, `taskNo` |
| `/api/parcels/route` | `{waybillNoList: [≤100], pageNum, pageSize}` | `{list, total}`: `waybillNo`, `destinationCenter`, `courierWorkArea` (route), `packageNo` (bag it is in now: the hub bag once sorted, else its gaylord / pickup batch), `weight`, `orderStatusName` |
| `/api/dashboard/trucks` | working-day window `{centerId, centerIdEncryption, timeZone, startTime, endTime, …}` + `{type: 4` on the way / `6` arrived not unloaded / `7` unloaded`, pageNum, pageSize}` | `{records, total}`: `taskCode`, `placeOfOrigin`, `originTransitId`, `destinationTransitId`, `actualArrivalTime`, `estimationArrivalTime`, `unloadCompleteTime`, `bagCount`, `waybillCount`, `transportType`, `deadline` (inbound cutoff, 10 PM), `vehicleNo` |
| `/api/dashboard/sort-count` | working-day window | `{alreadySortedCount, alreadySortedBagCount, allShouldSortCount, tdShouldSortCount, hsShouldSortCount, allShouldPickCollectCount, allShouldPickTransferCount, allShouldPickCusPickupCount, allShouldPickClearanceCount, allPendingSignoutBagCount, allPendingSignoutCount}`. "Should sort" = piece-scanned freight waiting (pickup, drop-off, belt rejects), not the gaylords on the floor |
| `/api/dashboard/arrivals` | working-day window (usually tomorrow's) | `{vehicleEstimatedArrival, estimatedArrivalWaybillCount}` |
| `/api/dashboard/arrival-stats` | working-day window | `{waybillInTransit, waybillArrived}` |
| `/api/dashboard/arrival-hours` | window + `{type: 1}` | `[{hourRange, vehicleCount, trunkWaybillNoCount, branchWaybillNoCount}]` |
| `/api/history/daily-totals` | `{startDate, endDate}` | `[{day, total}]`: packages per past working day (the forecast's history) |
| `/api/dispatch/departures` | `{}` (no auth; `chat_api` reads it for a shared-table search) | `{items: [{record_id, fields}], total, has_more}`, one per outbound truck. `fields`: `Departure date` (ms, midnight of the operating day), `Shift time` (same), `Destination`, `Truck type` (`53FT` / `26FT`), `Pickup time` (ms, planned departure), `Task no.` (`TT…` once dispatch made the task, else the planned clock time `HH:MM`), `Driver`, `Dock`, `Sent` (bool), `Cancelled` (`Yes`) |

## Sort record reason codes

`refluxReason` → `refluxReasonName`: `CHUTE_FULL` Chute full · `OVER_LOOP_LIMIT` Over loop limit · `NOT_DROPPED` Not
dropped · `CHUTE_NO_BAG` No bag on chute · `NO_MATCHED_RULE` No matched rule · `NOREAD` No read ·
`WAYBILL_NOT_FOUND` Waybill not found · `INTERCEPTED` Intercepted. Successful sorts have an empty reason.

Exception chutes: NOREAD `001`/`109`/`214`, reject (over the loop limit) `003`, interception `146`, no match `147`.
Hub chutes: `PHX.H` 049 + 188, `STL.H` 050 + 189 … `SAN.H` 056 + 195. Station routes fill the rest from 004 up.

## The synthetic night (hubsim/model.py)

Seeded by the date, so a day always looks the same. About 200,000 packages a night (weekday-dependent): ~12 linehaul
trucks of 25–40 gaylords from the other hubs plus six pickup batches. The sorter runs 4 PM–5:15 AM at 17–23k
parcels an hour over 30 feed seats (two slow seats and seat 16 with a high reject rate each night). Scripted around
"now": three jammed chutes (one cleared an hour ago), a no-rule batch for a route missing from the plan, lost labels
(a second label 2–6 pieces minutes after a full bag), one bag of 850+ pieces on a hub chute most nights, a truck in
the yard never scanned in, one being unloaded, one unloaded without scans, three still on the road, and outbound
trucks at the dock, departed and due.

`--clock auto` (default) runs the night on the real clock between 5:30 PM and 4:30 AM and otherwise shifts it by
whole hours so that "now" is about 10 PM. While running, the server also writes the hourly sort report feed to
`data/hourly-report.json` (`hubsim/report.py`, refreshed after every :16; `--no-report` turns it off).
