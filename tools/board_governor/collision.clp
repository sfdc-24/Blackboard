;;; Lane arbitration for the Blackboard: who owns a dispatch, and who collided.
;;;
;;; WHY THIS IS RULES AND NOT AN IF-STATEMENT
;;;   On 2026-09-15 three surfaces took one fix inside six minutes:
;;;   claude-code-cli claimed at 21:01:27Z, vm-claude-code-cli at 21:03:45Z, and
;;;   codex opened a PR at 21:07Z with no claim row at all. Nobody did anything
;;;   wrong; there was simply no mechanism that could say "this one is first".
;;;   Two more happened on 09-09. A rule engine can answer that in milliseconds
;;;   and say WHICH facts it matched, which is the part a large model argues
;;;   about at length and at cost.
;;;
;;; WHAT IT DELIBERATELY DOES NOT DO
;;;   It does not re-dispatch, reassign, or write to the board. A COLLISION row
;;;   cannot recall a duplicate that already dispatched, and silence is not proof
;;;   that a worker's last effect stopped - Codex's review of the re-evaluation
;;;   (COLLAB-20260915-PR112-R1) is right about that. This file only names an
;;;   owner and reports later claimants. Acting on it needs the admission and
;;;   fencing contract in ARCH-20260914, which does not exist yet.
;;;
;;; OUTPUT CONTRACT (parsed by governor.py; keep it pipe-delimited and stable)
;;;   OWNER|<target>|<tag>|<row-id>|<epoch-seconds>
;;;   COLLISION|<target>|<owner-tag>|<owner-row>|<collider-tag>|<collider-row>|<gap-seconds>
;;;   UNANSWERED|<bcb-id>|<phase>|<from-tag>|<row-id>|<quiet-minutes>

(deftemplate claim
   (slot row-id (type STRING))
   (slot bcb-id (type STRING))
   (slot tag (type STRING))
   (slot target (type STRING))
   (slot ts (type INTEGER)))

;;; An ask: work handed to a named lane that somebody is expected to pick up.
;;; DISPATCH and REVIEW_REQUEST are the same thing here.
(deftemplate ask
   (slot row-id (type STRING))
   (slot bcb-id (type STRING))
   (slot tag (type STRING))
   (slot phase (type STRING))
   (slot ts (type INTEGER)))

;;; Any row that carries answers=<id>, whatever its own phase. Most asks are
;;; answered by a REVIEW_RESULT or RESULT, not by a CLAIM.
(deftemplate answered
   (slot id (type STRING))
   (slot by (type STRING))
   (slot ts (type INTEGER)))

;;; The earliest claim on a target owns it.
;;;
;;; The (not (claim ... earlier)) guard is what makes this correct with three or
;;; more claimants. Without it, the pair (second, third) also matches and would
;;; name the SECOND claimant as owner - the rule would report a collision with
;;; the wrong winner, which is worse than reporting nothing.
(defrule owner
   (claim (row-id ?row) (tag ?tag) (target ?target&~"") (ts ?ts))
   (not (claim (target ?target) (ts ?earlier&:(< ?earlier ?ts))))
   =>
   (printout t "OWNER|" ?target "|" ?tag "|" ?row "|" ?ts crlf))

;;; A later claim by a DIFFERENT tag on an owned target is a collision.
;;;
;;; Same-tag reclaims are not collisions: two sessions share the claude-code-cli
;;; tag on this laptop, and a tag re-asserting itself is noise, not contention.
;;; That is a known limitation, recorded rather than hidden: tag is a claimed
;;; lane, not an identity, so this rule cannot separate two sessions behind one
;;; tag. Distinguishing them needs verified session identity, which the board
;;; does not carry today.
(defrule collision
   (claim (row-id ?owner-row) (tag ?owner-tag) (target ?target&~"") (ts ?owner-ts))
   (not (claim (target ?target) (ts ?earlier&:(< ?earlier ?owner-ts))))
   (claim (row-id ?other-row) (tag ?other-tag&~?owner-tag) (target ?target) (ts ?other-ts&:(> ?other-ts ?owner-ts)))
   =>
   (printout t "COLLISION|" ?target "|" ?owner-tag "|" ?owner-row "|"
                ?other-tag "|" ?other-row "|" (- ?other-ts ?owner-ts) crlf))

;;; An ask nobody answered, once enough board time has passed.
;;;
;;; THIS IS THE RULE THE 2026-09-16 MISS WOULD HAVE CAUGHT.
;;;   CODEX-01A09BF0-PROTOTYPE-CONTRACT-REVIEW asked claude-code-cli for a
;;;   review. The reader that was supposed to surface it could not see the
;;;   addressing, so it sat unanswered and was found by a human reading rows by
;;;   hand. A collision rule cannot catch that - nobody claimed it twice, nobody
;;;   claimed it at all. Silence is the failure, and silence needs its own rule.
;;;
;;; THE THRESHOLD IS BOARD TIME, NOT WALL TIME.
;;;   3600 seconds behind the newest row on the board. Replaying a dump from
;;;   last week must give the verdicts it would have given then; a wall-clock
;;;   comparison would declare every historical ask stale and say nothing.
;;;
;;; WHAT IT DELIBERATELY DOES NOT DO
;;;   It does not re-dispatch or nag. It names an ask and how long it has been
;;;   quiet. Acting on that needs the admission and fencing contract that does
;;;   not exist yet - the same boundary the collision rule respects.
;;; THE GRAMMAR FLOOR, AND WHY IT IS A FACT AND NOT A PYTHON FILTER
;;;   answers= is a convention that started on 2026-09-04T22:29Z, 1107 rows into
;;;   the board. An ask older than that can never be marked answered, however
;;;   well it was answered. Without this guard the rule reported 116 unanswered
;;;   asks of which 35 were pure artifact, and the answered-rate by week reads
;;;   0% / 37% / 73% across W36 / W37 / W38 - the 0% week is entirely rows the
;;;   convention could not describe.
;;;
;;;   It is a FACT because the whole reason this is CLIPS is that the engine can
;;;   say which facts it matched. A floor hidden in Python is a verdict nobody
;;;   can audit from the output.
;;;
;;;   NOTE the residual, rather than implying it away: W37 sits at 37% adoption,
;;;   so asks from that week still over-report. That is a real adoption gap, not
;;;   an artifact, and it is reported as-is.
(defrule unanswered-ask
   (now ?now)
   (grammar-epoch ?epoch)
   (ask (row-id ?row) (bcb-id ?id) (tag ?from) (phase ?phase)
        (ts ?ts&:(< ?ts (- ?now 3600))&:(> ?ts ?epoch)))
   (not (answered (id ?id)))
   =>
   (printout t "UNANSWERED|" ?id "|" ?phase "|" ?from "|" ?row "|"
                (integer (/ (- ?now ?ts) 60)) crlf))
