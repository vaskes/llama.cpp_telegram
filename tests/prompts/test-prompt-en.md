Build a complete, single-file HTML/Three.js experience of a beautiful, interactive Newton's cradle.

Use the attached image as reference: five polished metal balls suspended between two parallel chrome frames.

The result should look like a luxury product filmed in a real photography studio, with convincing materials, smooth pendulum motion, and working collision physics.

Open directly into the finished scene. The first rendered frame must already be composed well enough to use as a video thumbnail.

Prioritize three things equally: the beauty of the chrome, the physical construction, and the correctness of the motion.

Center one substantial Newton's cradle on a seamless warm-ivory studio surface, filling most of the image. Leave comfortable margins for the full outward swing on both sides.

Construct exactly five identical, perfectly round polished stainless-steel spheres.

At rest, their centers must form one horizontal row, with neighboring spheres touching. Their diameters, masses, and suspension lengths must match.

Use two slender, parallel, inverted-U chrome support frames with rounded corners, positioned in front of and behind the ball row.

Each ball must hang from two fine, taut suspension wires: one connected to the front support and one to the rear support. These wires constrain its motion to the shared vertical swing plane.

Include small, believable attachment details. The wires must connect visibly to the balls and frame.

The support frame and anchors remain completely stationary. Update the suspension geometry from the actual ball positions every frame. Wires must never stretch, disconnect, pass through the spheres, or wave independently.

Choose elegant proportions: substantial spheres, slender tubular supports, enough suspension length for a satisfying swing. Avoid thick ropes, oversized frames, and a bulky decorative base.

Make the metal look convincingly reflective, with PBR materials, restrained roughness, and a procedurally built reflection environment.

Create large studio-softbox reflections that wrap naturally around the spheres and curved frame. Include broad bright highlights, darker reflected regions, and subtle warm tones from the studio.

The spheres must retain readable volume. Avoid flat gray metal, featureless white balls, or excessive bloom.

Keep the background uncluttered, with a very subtle ivory gradient. The overall image should feel calm, precise, and expensive.

Implement a working simplified Newton's cradle simulation.

Represent each sphere as an equal-mass pendulum with an angular position and angular velocity. Gravity must drive the swing. Derive the sphere positions from the pendulum geometry.

Use stable time integration and event-based impact handling that propagates the impulse through the full chain of touching balls.

Release one outer ball. It swings into the row, transfers its motion through the chain, and approximately one ball swings outward at the opposite end.

Handle the chain of contacts consistently. A single order-dependent collision pass must not produce a different result when the direction reverses. Collisions should approximately conserve momentum and kinetic energy, with small losses.

Use gentle damping so the swing amplitude decreases gradually. The simulation must not gain energy, accelerate indefinitely, visibly interpenetrate, jitter continuously, or send the entire row swinging after every impact.

Start with the leftmost ball raised approximately 35 degrees and the other four hanging at rest. Hold this initial pose briefly, then release automatically. The experience must immediately demonstrate the classic alternating motion without requiring a click.

Allow the user to drag either outer ball outward within its permitted swing plane, then release it. Clamp the drag angle to a sensible maximum, around 45 degrees. Keep its suspension wires taut throughout the drag.

Deliver one complete HTML file with inline CSS and JavaScript. Keep the experience silent: no music, collision sounds, particles, motion trails, impact flashes, or decorative text.