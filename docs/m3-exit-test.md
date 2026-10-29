# M3 exit test: a real driver, 5 stops, signal dropped mid-route

Exit criterion (Phase 1 Delivery Specification, M3): *a real driver completes a 5-stop trip with signal dropped
mid-route; PODs appear in the portal; 250-vehicle load test passes.* The load test passed in
[loadtest-m3.md](loadtest-m3.md). This runbook covers the rest.

Staging: `https://154-65-99-61.sslip.io`. Server commands run as `deploy` in `/opt/logistics`
(`sudo -iu deploy`, then `cd /opt/logistics`).

## What passes

1. The driver delivers all 5 stops in the driver app, with a signature or photo at each.
2. Stops 3 and 4 are recorded with **no signal**, and the app is closed and reopened while offline. Once the phone
   is back online they sync by themselves, and nothing is refused.
3. The driver finishes the trip in the app.
4. `exit_test_trip report` prints `PASS (server side)`, with stops 3 and 4 marked `synced late`.
5. Signed in to the portal as the client, every POD is under **Tracking** and **Documents**. The signature and
   photos open and download. Stops 3 and 4 show the time they were signed at the stop, not the time they synced.
6. Each order in the portal has the status *Delivered*.

Not part of this test: live vehicle positions and ETAs. Those need Powerfleet MiX credentials, so the portal shows
no ETA and the driver taps **Arrived** at each stop.

## People and kit

- **You**: staging, loading and dispatch, and watching the portal.
- **A driver** with an Android or iPhone on automatic date and time, charged, with mobile data.
- **Five real places** the driver can stop at on one loop, such as shops or filling stations. The recipient can be
  anyone there, or the driver.

## Before the day (about 30 minutes)

1. **Deploy the latest `main`** and, if not done since the shipments release, back-fill once:

   ```bash
   deploy/ops.sh deploy
   deploy/ops.sh manage backfill_shipments
   ```

   `seed_demo` must have run already. The test uses its client KAL, warehouse DEMO-JHB and the
   `demo.operator` and `demo.client` logins.

2. **Write the five stops** to a CSV on the server, in driving order. Get each place's coordinates from Google Maps:
   long-press the place, then copy the numbers. Codes are at most 20 characters.

   ```bash
   nano ~/stops.csv
   ```

   ```csv
   code,name,address,latitude,longitude
   EXT-1,Engen Kempton,12 Pretoria Rd Kempton Park,-26.101234,28.231234
   EXT-2,...
   ```

3. **Create the orders, the driver and the vehicle.** Each address gets one order for one case of KAL-TEA-80. The
   command picks and packs it into one carton and creates the driver login.

   ```bash
   deploy/ops.sh manage seed_exit_test --not-production --stops - \
     --driver-email driver.one@nuronteq.test --driver-name "First Last" --vehicle "ABC 123 GP" < ~/stops.csv
   ```

   It prints the batch (for example `EXIT-1103140005`), the five orders and their carton numbers (SSCCs), and the
   driver's password **once**. Give the password to the driver in person. Don't post it in a chat. The command
   ends with the next command to run.

4. **Plan the trip** with the printed command:

   ```bash
   deploy/ops.sh manage exit_test_trip plan --not-production --batch EXIT-1103140005 \
     --vehicle "ABC 123 GP" --driver-email driver.one@nuronteq.test
   ```

   This prints the trip number (`TRP-…`), and the stops in CSV order with their cartons.

Optional rehearsal on your own phone: run steps 3 and 4 with your own driver email, and add `--load-and-dispatch` to
`plan`. Finish that trip in the app before the real one, because a vehicle or driver still out on a trip can't be
dispatched again.

## On the day

### Load and dispatch (you)

1. Open `/scanner/` and sign in as `demo.operator@nuronteq.test`. Choose site DEMO-JHB.
2. Go to **Load**, open the trip, and type or scan each of the 5 SSCCs until it shows **5 of 5 units loaded**.
3. Dispatch:

   ```bash
   deploy/ops.sh manage exit_test_trip dispatch --not-production TRP-…
   ```

### Set up the driver's phone (with signal, before leaving)

1. Open `https://154-65-99-61.sslip.io/scanner/` and add it to the home screen (Android: menu → *Add to home
   screen*; iPhone: Share → *Add to Home Screen*). Open it from the home-screen icon from now on.
2. Sign in with the driver login. You need signal to sign in. Allow **location** and **camera** when asked.
3. The trip shows as *dispatched*, with 5 stops in order. The app keeps the whole trip on the phone, so it
   works without signal from here on.

### The drive

| Leg | Signal | What the driver does | What to check |
|---|---|---|---|
| Stops 1 and 2 | On | Open the stop → **Arrived** → **Deliver** → **Mark all delivered** → recipient's name → signature. At stop 2, also take a photo. → **Confirm delivery**. | The bar shows *Online*. **Activity** shows nothing waiting. In the portal, the shipment turns *Delivered* within a minute. |
| After stop 2 | **Airplane mode on**, location still on | Note the time. | The bar shows *Offline*. |
| Stops 3 and 4 | Off | The same steps. The app says *Delivery saved. It will be sent when you have signal.* At stop 4, **swipe the app closed, then reopen it** from the home screen. | After reopening, the trip and both deliveries are still there, and **Activity** shows them waiting to send. In the portal, stops 3 and 4 are still *in transit*. |
| Before stop 5 | **Airplane mode off** | Wait about 15 seconds, or tap **Sync now**. Note the time. | **Activity**: nothing waiting, and no *Not accepted* section. In the portal, stops 3 and 4 turn *Delivered*. |
| Stop 5 | On | Deliver as before. | |
| End | On | Open the trip → **Finish trip**. | The trip shows *completed*. |

Optional extra: make stop 5 a failed delivery to try the return path. Use **Could not deliver** with a reason and a
photo, and bring the carton back. The report still passes, because every stop has a record. A redelivery needs a new
trip, and that is outside this test.

## Check the result

1. **Server report:**

   ```bash
   deploy/ops.sh manage exit_test_trip report TRP-…
   ```

   It needs a `PASS (server side)` line, with `synced late` on stops 3 and 4. Look at any `check:` lines: a POD
   recorded more than 500 m from its address usually means poor GPS where it was signed. That is worth noting, but
   it isn't a failure.

2. **Portal:** sign in at `/portal/` as `demo.client@nuronteq.test`.
   - **Tracking**: open each of the 5 shipments. Each one is *Delivered*, with the recipient, the signature and the
     photos, and downloads work. The signed times of stops 3 and 4 are the times at the stop, before you switched
     airplane mode off.
   - **Documents**: search the batch (for example `EXIT-1103140005`). Every signature and photo is listed and opens.
   - **Orders**: all five orders of the batch are *Delivered*. Open one: its timeline shows the dispatch and the
     delivery.

3. Fill in the record below and keep it with the M3 sign-off.

| | Result |
|---|---|
| Date, driver, phone model and browser | |
| Trip and batch | |
| Airplane mode on / off (times) | |
| Report: PASS line present, stops 3 and 4 `synced late` | |
| Portal: 5 PODs under Tracking, signatures and photos open | |
| Portal: Documents search finds them | |
| Orders are *Delivered* | |
| Anything refused (*Not accepted*) or odd | |

## If something goes wrong

- **Sign-in fails**: sign-in needs signal. Sign in before leaving the depot.
- **"The device clock is off"**: the phone's date or time is wrong. Turn on automatic date and time. The refused
  delivery changed nothing, so record it again.
- **Dispatch says "Not loaded"**: a carton wasn't scanned. Load it on the Load tile and dispatch again.
- **Dispatch says "Resource busy"**: the vehicle or driver is still on an earlier trip, such as a rehearsal. Finish
  that trip in the app first.
- **The report says "no POD was synced late"**: the phone had signal at stops 3 and 4, or was offline for less than
  2 minutes. Run it again with a new batch: repeat steps 3 and 4 of the preparation. The same addresses are reused,
  and each batch gets new orders.
- **Stops 3 and 4 never sync**: check **Activity** on the phone. Anything under *Not accepted* says why. Note it in
  the record before dismissing it.

## Afterwards

The test data belongs to the fictional client KAL, on staging only. The driver login and person record hold a real
name. If the driver won't keep using staging, set the login to inactive in the admin (**Users**).
