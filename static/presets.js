// presets.js
// Quick-pick angles shown above the Rotate button. Tapping one fills in the angle
// (and the direction, if given). It never starts a move by itself.
//
// Each entry:
//   degrees    required. Greater than 0, no more than 360.
//   direction  optional. 'CW' or 'CCW'. Leave it out to keep whatever is selected.
//   label      optional. Button text. Defaults to the angle, e.g. "90°" or "90° CW".
//
// Examples (delete the leading // to turn one on):
window.TURNTABLE_PRESETS = [
  // { degrees: 45 },
  // { degrees: 90 },
  // { degrees: 180, direction: 'CW' },
  // { degrees: 60, label: 'Index 6' },
];