# Bootstrap Icons v1.13.1

Official sources: https://github.com/twbs/icons/tree/v1.13.1/icons

Only the icons used/available in Playback are included. The original SVG files
and the upstream MIT LICENSE are preserved. `sprite.png` is generated from these
SVGs with separate 20/25/30/40 px rasterizations, two foreground colors and a
transparent background. `sprite.json` maps each name/state/size to its crop.

Regenerate with `tools/fetch_playback_icons.ps1` followed by
`node tools/generate_playback_icons.cjs` (development dependency sharp 0.35.4).
Neither Node nor sharp is needed to run the application; Tk reads the local PNG.
