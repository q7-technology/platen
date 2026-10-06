# The toy town: a plan

Platen's next big piece of work. A live, game-like view of every site,
printer and job, plus page printing alongside labels. Built in three steps;
each one is useful on its own.

## Decisions so far

- **Who looks at it:** warehouse staff, managers, and the administrator.
  Same scene for everyone, different side panel and buttons.
- **Several sites.** A world map of sites; dive into one to see its printers.
  Staff land straight inside their own site.
- **Bigger moves.** Drag a job from one printer to another, box-select many,
  re-route a whole printer, and undo any of it.
- **Who may move what:**
  - an operator belongs to one site and may move jobs inside it
  - a manager looks after any sites an administrator ticks, and may move jobs
    between them and re-route a printer
  - an administrator sees and does everything, including templates, data and
    logs
- **Labels and pages.** Zebra label printers and ordinary office printers.
- **Pages get their own editor:** header, footer, and tables bound to a query
  that grow and spill onto the next page with their headings repeated.
- **Dark toy town.** Follows the Q7 Technology design system: night-sky
  ground, glowing blue outlines, gold for "look here", labels stay white.
- **Drawn flat at a fixed angle,** not real 3D. Zoom and pan, no spinning.
  Light enough for an old warehouse PC or a tablet.
- **Foundations first.**

## Step 1 — foundations

- **Sites.** *Done.* A printer belongs to a site. A printer in no site is shared:
  everyone can use it, which is how an install with no sites keeps working.
- **A manager role.** *Done.* Operators have at most one site, managers any number,
  administrators all of them. Every route that touches a printer or a run
  checks the site as well as the role.
- **Grid positions** for printers, ready for the site view. *Done* for
  printers (`PUT /printers/{id}/position`); agents can follow.
- **Live updates.** *Done.* `/events`, a one-way stream fed by the worker and
  the API through Redis, scoped to the sites the viewer looks after.
- **Printer health.** *Done.* Every 30 seconds each Zebra is asked `~HQES`:
  out of labels, head open, out of ribbon, labels nearly out. That is what
  turns a printer gold or red.
- **Moving a run safely.** *Done.*
  - only between labels, like cancel and the queue hold
  - only labels with no `printed_at` move; nothing prints twice
  - a run only moves to a printer at its template's dpi. Re-rendering for
    another pitch would re-read the data, and the labels could then differ
    from the ones that were checked, so it is refused with a reason instead
  - labels never go to a page printer, nor pages to a label printer (lands
    with page printing in step 3)
  - a hand-fed (`waiting`) run asks before it moves
  - every move is audited; undo moves the remaining labels back
- **Detours.** *Done.* "Send everything for printer 3 to printer 4 until I say."
  New runs follow the detour until it is cleared.

## Step 2 — the toy town

- World map: sites glowing on the star field, jobs flying between them.
  A manager's own sites are bright, the rest dimmed.
- Site view: printers on the grid, belts with moving dashes, white label
  parcels riding along. Printers blink while working.
- Scoreboard along the top, a side panel that depends on who is looking,
  a job tracker along the bottom (received → rendered → queued → printing →
  done).
- Drag and drop, box-select, undo, a detour sign on a re-routed belt, and a
  small sparkle when the queue is empty.
- Isometric SVG, vanilla DOM, no build step, like the other studio screens.

## Step 3 — page printing

- A page template: paper size, margins, header, footer, a flowing body, and
  tables bound to a query that split across pages with repeated headings.
- Rendered to PDF with ReportLab, whose tables already know how to split.
  The preview comes from the same renderer, so it is what will print.
- Office printers through the routes Platen already has: raw 9100, CUPS,
  or the desk agent.
- A label run counts printed per label; a page job counts per document,
  because an office printer takes the whole PDF at once.
- Tall office printers and paper stacks join the toy town.

## Rules to add to CLAUDE.md as they land

- Every route that touches a printer or a run checks the viewer's sites.
- A move never sends a label that has already printed.
- A move re-renders before it moves, or does not move.
