// Build identity for the live read-back (case 00001051). scripts/gas_build_readback.py --stamp writes the
// deploying commit here before clasp push; the no-secret ping returns it, so a stale republish is detectable.
var BUILD_SHA = 'unset';
