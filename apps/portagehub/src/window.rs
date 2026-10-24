//! The window's size: big enough for the pages, never bigger than the screen it opens on.

/// The size PortageHub asks for (points).
pub const WANTED: (f32, f32) = (1380.0, 860.0);
/// The smallest size the pages are laid out for; laptop panels (1366x768, 1280x720) are above it.
pub const MIN: (f32, f32) = (800.0, 560.0);
/// Share of the monitor a starting window may take, leaving room for panels and title bars.
const SHARE: f32 = 0.95;

/// `requested` shrunk to fit `monitor`, but never below [`MIN`].
pub fn fit(requested: (f32, f32), monitor: (f32, f32)) -> (f32, f32) {
    (requested.0.min(monitor.0 * SHARE).max(MIN.0), requested.1.min(monitor.1 * SHARE).max(MIN.1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn laptop_panels_get_a_window_that_fits() {
        let (w, h) = fit(WANTED, (1366.0, 768.0));
        assert!(w <= 1366.0 * 0.95 && h <= 768.0 * 0.95, "{w}x{h}");
        assert!((w, h) == (1366.0 * 0.95, 768.0 * 0.95), "the width and the height both give way: {w}x{h}");
        let (w, h) = fit(WANTED, (1280.0, 720.0));
        assert!(w <= 1216.0 && h <= 684.0, "{w}x{h}");
        assert!(h >= MIN.1 && w >= MIN.0, "the pages stay usable");
    }

    #[test]
    fn big_screens_keep_the_wanted_size() {
        assert_eq!(fit(WANTED, (1920.0, 1080.0)), WANTED);
        assert_eq!(fit(WANTED, (3840.0, 2160.0)), WANTED);
        // Only the dimension that does not fit gives way.
        assert_eq!(fit(WANTED, (2560.0, 800.0)), (1380.0, 760.0));
    }

    #[test]
    fn a_tiny_or_odd_monitor_never_goes_below_the_minimum() {
        assert_eq!(fit(WANTED, (640.0, 480.0)), MIN);
        assert_eq!(fit(WANTED, (0.0, 0.0)), MIN);
    }
}
