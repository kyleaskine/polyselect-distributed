"""polylocal — a local, one-shot GNFS polyselect runner that closes the loop with
aliquot-tracker without the distributed coordinator.

`specify an AS -> fetch its composite from the tracker -> run the msieve coefficient search
locally -> (optionally) optimize and submit the best polynomial back`.

This is the small first step toward the "client just grabs (composite, coefficient) work and
sends it back" system; it deliberately runs everything on one GPU+CPU box and changes neither
`polyserver/` nor aliquot-tracker. See the plan in DESIGN.md's companion / the repo plan file.
"""
