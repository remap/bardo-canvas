# track.mp3

A synthesized ambient placeholder (three sine waves, a fifth + octave, faded
in/out so it loops cleanly) — not a licensed music track. Generated with:

```bash
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "sine=frequency=110:duration=30" \
  -f lavfi -i "sine=frequency=164.81:duration=30" \
  -f lavfi -i "sine=frequency=220:duration=30" \
  -filter_complex "[0:a]volume=0.25[a0];[1:a]volume=0.2[a1];[2:a]volume=0.15[a2];[a0][a1][a2]amix=inputs=3:duration=longest[mixed];[mixed]afade=t=in:st=0:d=2,afade=t=out:st=28:d=2[out]" \
  -map "[out]" -ac 2 -ar 44100 -b:a 128k apps/noraebang-generative/static/assets/track.mp3
```

Replace this file with a real track whenever you have one; no code
changes needed, the path is fixed but the content is not.
