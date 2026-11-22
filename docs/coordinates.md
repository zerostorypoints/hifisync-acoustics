# Coordinates

The engine uses the room geometry convention of HiFiSync. Every position the
engine stores or accepts (measurement points, node install positions, speaker
and listener positions in room snapshots) follows it.

- Unit: metres.
- x = 0 at the left wall, x = width at the right wall.
- y = 0 at the front wall (where the speakers are), y = length at the rear wall.
- z points up from the floor (an engine addition; HiFiSync's plan geometry is 2D).
- Rotation: degrees, 0 = facing +y, positive clockwise viewed from above.

## Room dimensions

A room stores width (x), length (y) and height (z). The `rooms` table columns
`width_m`, `length_m` and `height_m` map to those axes in that way: `width_m`
is the x extent, `length_m` the y extent, `height_m` the z extent. The design
plan lists the columns in the order `length_m, width_m, height_m`; the
order of listing has no meaning, the axis mapping above does.
`Store.upsert_room` takes them as keyword arguments to avoid mixing them up.
