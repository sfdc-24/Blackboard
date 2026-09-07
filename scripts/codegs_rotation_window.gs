/**
 * SFDC24 Blackboard Bus — DUAL-SECRET ROTATION WINDOW
 * claude-code-cli, 2026-09-03. For ISSUE 025 / GW-DIAG-003.
 *
 * WHY
 *   The bus authenticates on a single shared secret compared in two places
 *   (doGet and doPost). Change the Script Property and EVERY holder fails the
 *   same second — this machine, the Pipedream gateway, claude-mobile, and any
 *   instance nobody has inventoried. There is no zero-downtime rotation with a
 *   single-valued secret, which is very likely why ISSUE 025 was "rotated" by
 *   pasting the new value straight back into workflow source: the safe path
 *   did not exist, so the fast path won.
 *
 *   This patch creates the safe path. The gateway accepts EITHER the current
 *   secret or an optional previous one, so holders can be migrated one at a
 *   time with nothing broken in between. Delete the old property when the
 *   migration is done and the window closes.
 *
 * HOW TO APPLY (the bus is CONTAINER-BOUND — it is not in the standalone
 * project list. Open the sheet "Blackboard - Alpha DB" → Extensions → Apps
 * Script. That is the only way in; this cost an hour to discover on Sep 2.)
 *
 *   1. In getConfig_(), read the optional second property and return it.
 *   2. Paste authOk_() below anywhere at top level.
 *   3. Replace BOTH comparison sites with the authOk_ call.
 *   4. Save. No redeploy is needed for a Script Property change, but a CODE
 *      change does need Deploy → Manage deployments → edit → New version.
 *
 * ------------------------------------------------------------------
 * STEP 1 — in getConfig_(), after the existing BUS_SECRET line, add:
 *
 *   const secretOld = props.getProperty('BUS_SECRET_OLD'); // rotation window only
 *
 * and change the return to:
 *
 *   return { folderId: folderId, secret: secret, secretOld: secretOld };
 * ------------------------------------------------------------------
 * STEP 2 — paste this function:
 */

function authOk_(supplied, cfg) {
  if (!supplied) return false;
  if (supplied === cfg.secret) return true;
  // Rotation window: the previous secret keeps working until the property is
  // deleted. Deliberately opt-in — with no BUS_SECRET_OLD set this behaves
  // exactly as before, so applying the patch alone changes nothing.
  if (cfg.secretOld && supplied === cfg.secretOld) return true;
  return false;
}

/**
 * ------------------------------------------------------------------
 * STEP 3 — replace BOTH of these:
 *
 *   doGet,  around line 69:   if (params.secret !== cfg.secret) {
 *   doPost, around line 91:   if (body.secret !== cfg.secret) {
 *
 * with, respectively:
 *
 *   if (!authOk_(params.secret, cfg)) {
 *   if (!authOk_(body.secret, cfg)) {
 *
 * Leave the 401 bodies exactly as they are — the error text must not reveal
 * which of the two secrets was tried.
 * ------------------------------------------------------------------
 *
 * CUTOVER SEQUENCE — order matters, and each step is individually safe:
 *
 *   A. Apply this patch and deploy. Nothing changes yet (no OLD property set).
 *   B. Script Properties: set BUS_SECRET_OLD = <the current secret>,
 *      then set BUS_SECRET = <the new secret>. Both now work.
 *   C. Migrate holders one at a time, verifying each with a read:
 *        - Pipedream: add a BUS_SECRET environment variable with the new value,
 *          then change line 29 of send_whatsapp_reply from a hardcoded string
 *          to  const busSecret = process.env.BUS_SECRET;  and deploy.
 *        - This machine: set BUS_SECRET in .env, restart the capture loop
 *          (it reads .env once at process start).
 *        - Every other instance: hand them the new value out of band.
 *   D. Announce on the board that the window is closing.
 *   E. Delete BUS_SECRET_OLD. Any holder still on the old value now fails —
 *      which is the point: it surfaces holders nobody inventoried.
 *
 * DO NOT skip step B's ordering. Setting BUS_SECRET first and BUS_SECRET_OLD
 * second leaves a gap of a few seconds where only the new value works.
 */
