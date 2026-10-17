"""The dashboard's cards, a module each, here or in ~/.config/deck/cards/ (a module, or a folder with an
__init__.py). dash.py draws the ones the config turns on (dash_common.load_cards).

Every card has:
  TITLE, TITLE_ICON   its title and the icon before it
  a docstring         "Title: what it shows", which the card picker (,) lists it with
  render(data, inner, rows, keys)
                      a dict with its "lines" (inner columns wide), and optionally its "note" (next to
                      the title), "targets" (line, first column, end column, target) to click, None
                      columns spanning the whole row, and "links" (letter -> url, opened by that key,
                      taken from keys in order); or None to leave the card out of this frame. rows is
                      how many lines it may use, None for as many as it needs.

And, when it needs them (functions it defines itself: one it imports under these names doesn't count):
  SOURCES             {name: function} it reads every frame, in parallel; SLOW_SOURCES every SLOW_CHECK
                      seconds. Cards share them by name, and find them all in data, with "agents".
  refresh(data, force)  starts its background fetches (GitHub)
  fetched_at(data)    when its GitHub data was fetched, for the footer
  alerts(data)        chips for the greeting line, or (chip, target); ALERT_ORDER places them, lower first
  HINT                its keys, for the footer
  key(k)              a key pressed; True when it took it
  act(target)         one of its targets clicked
  SIDE                drawn SIDE_WIDTH wide next to the card before it when there's room (without letters)
  min_rows(data)      the lines it needs at least when a SIDE card stands next to it
  FLEX                gets the lines the other cards leave in the window
  place(path)         a short name for a folder, for other cards (Sessions); None when it isn't one of its own
  pr_tag(pr, data)    a few words for your PR's row in My PRs; "" when it has nothing to say
"""

import os

import dash_common

# Your own cards, a module or a folder each, go in the cards folder next to the config
__path__.append(os.path.join(os.path.dirname(dash_common.CONFIG), "cards"))
