# XinputReader
Use a gamepad to drive anything in Blender, on **Windows** and **Linux**!

The add-on is made to work with an XBox style gamepad and some minor alteration might be needed if the relevant buttons are not found on your controller.

One button triggers the creation of an empty object with custom properties matching the button inputs. These can be used as drivers for any type of control within Blender.

There is a second button that creates a geometry nodes group and sets up drivers to all the sockets so it can be used as an easy control hub.

**Record Controller** plays the animation and keys every input on every frame as it goes, so a take can be replayed without baking whatever it drives. See [Recording](#recording).

This add-on is **not** heavily tested but you're welcome to fork and adjust as required!

# Installation

Download the add-on as a zip and install it through *Edit > Preferences > Get Extensions > Install from Disk*.

~~Install the XInput library via pip using the button in the add-on preferences.
Toggle system console to check if it succeeded or errored. If you see a lot of red, you'll need to run Blender as an administrator.
If you still see red, try downloading a portable version of Blender.~~

(Following 4.2 extension conventions, the python wheel is now bundled)

The add-on preferences list the controllers that were found and let you pick which one to read, in case you have more than one plugged in.

Runs as a modal operator so take care with the lack of auto-saves!!

Good luck!

# Recording

*Record Controller* starts the playback and writes a keyframe for all twenty inputs on every frame that plays, so your performance is captured as animation on the **XInput Reader** empty. Everything that was driven by the live inputs is then driven by the recorded ones, which means a rigid body simulation can be re-run from the recording instead of being baked to keyframes.

The recording stops on its own at the last frame of the scene, when you stop the playback, or when you press Esc or right click, whichever comes first. The frame range that was captured is shown in the panel, with a button to throw the take away.

Two options next to the button:

* **Start From First Frame** jumps back to the first frame before recording. Simulations have to be played from their start to be correct, so leave this on unless you are recording onto the end of a take.
* **Clear Previous Recording** throws away the earlier take instead of recording over the part of it that gets played through. With this off, a shorter second take leaves the tail of the first one behind.

Buttons are keyed with constant interpolation and the sticks and triggers with linear interpolation, so a press lands on exactly the frame it happened.

Some things worth knowing:

* Recording puts the **XInput Reader** empty in the scene if it is not already there. Blender does not evaluate objects outside of the scene, so its keyframes would otherwise never play back.
* Afterwards use the playback, not *Monitor Controller*, to watch the take. Monitoring writes the live controller values over the recorded ones as it runs.
* Recording samples the controller as fast as the modal operator is served and keys the newest values on each new frame, so a take is as smooth as the playback that captured it. If the playback drops below the scene frame rate you get keys on the frames that were actually played.

# Rigid bodies

Blender does not notice when a rigid body constraint is changed by a driver. The simulated frames it already has are replayed exactly as they were, so a second take looks identical to the first one and the simulation appears stuck. Emptying the point cache does not help either, which is what the two buttons under *Rigid Body Cache* are for:

* **Free** throws the simulated frames away, so playing again simulates from scratch. Use it after changing anything the controller drives.
* **Bake** does the same and then simulates the whole scene frame range in one go. Only a baked simulation can be scrubbed or rendered out of order, since jumping to a frame never simulates anything by itself.

*Bake* also sets the cache frame range to the scene frame range. The rigid body cache keeps a range of its own, which starts out as 1 to 250 no matter how long the scene is, and the simulation stops dead at the end of it.

Recording resets the simulation on its own when *Start From First Frame* is on, so each take is simulated from its beginning with the inputs you are giving it. Recording onto the end of an earlier take cannot do that and says so, since the simulation of the earlier frames is only in the cache.

If you know Blender's physics internals: what actually resets the cache is writing one of the rigid body world's own settings, so the add-on writes `substeps_per_frame` back onto itself. `ptcache.free_bake` alone does not do it.

# Platforms

| | how the gamepad is read |
| --- | --- |
| Windows | the bundled [XInput-Python](https://pypi.org/project/XInput-Python/) wheel |
| Linux | the kernel's evdev devices in `/dev/input`, no extra library needed |

Both give the same properties, with the same names, ranges and dead zones, so a blend file set up on one platform works on the other.

## Linux notes

* Any controller the kernel knows about works, whether it is handled by `xpad` (XBox), `hid-playstation`, or the generic HID driver. It gets picked up as soon as it is plugged in, even while the monitor operator is already running, and unplugging it just freezes the last values.
* Blender needs to be allowed to read the controller. Desktop sessions normally hand the logged in user access to any gamepad automatically. If the add-on preferences say the device is not readable, add yourself to the `input` group and log back in:

  ```sh
  sudo usermod -a -G input $USER
  ```

* Face buttons are the one thing Linux drivers do not agree on. The codes used here follow the `xpad` convention, which is what XBox controllers report. If your controller has X and Y (or A and B) the other way around, swap the names in the `_EVDEV_BUTTONS` table at the top of the Linux section in `gamepad.py`.
* Controllers connected through Steam Input are presented to the system as a virtual XBox pad, which works the same way.

macOS is not supported, neither backend exists there.
