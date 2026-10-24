//! Which renderer opens the window: wgpu (Vulkan or OpenGL, the default) or plain OpenGL (glow).
//!
//! wgpu needs a Vulkan driver or a reasonably modern OpenGL. Older graphics hardware and some
//! virtual machines only have legacy OpenGL, which the glow renderer can use. In the default `auto`
//! mode PortageHub tries wgpu first and, when that cannot open a window, starts itself once more
//! with glow (a second event loop cannot be created inside the same process, so it is a restart).
//! `PORTAGEHUB_RENDERER=wgpu` or `=glow` pins one renderer and never falls back.

/// The environment variable that picks the renderer.
pub const VAR: &str = "PORTAGEHUB_RENDERER";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Choice {
    Auto,
    Wgpu,
    Glow,
}

impl Choice {
    /// From the value of [`VAR`] (None when unset). A value that is not understood is an error
    /// message, so a typo is reported instead of silently ignored.
    pub fn parse(value: Option<&str>) -> Result<Choice, String> {
        match value.map(|v| v.trim().to_ascii_lowercase()).as_deref() {
            None | Some("") | Some("auto") => Ok(Choice::Auto),
            Some("wgpu") => Ok(Choice::Wgpu),
            Some("glow") => Ok(Choice::Glow),
            Some(other) => Err(format!("{VAR}={other} is not one of auto, wgpu, glow")),
        }
    }

    /// The renderer of the first attempt. eframe is told explicitly: with both renderers built in
    /// its own default is a detail of the library.
    pub fn first(self) -> eframe::Renderer {
        match self {
            Choice::Glow => eframe::Renderer::Glow,
            Choice::Auto | Choice::Wgpu => eframe::Renderer::Wgpu,
        }
    }

    /// Whether a window that could not be opened is followed by an attempt with glow. Without a
    /// display neither renderer can work, so there is nothing to retry.
    pub fn falls_back(self, has_display: bool) -> bool {
        self == Choice::Auto && has_display
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_variable_is_read_case_insensitively_and_typos_are_reported() {
        assert_eq!(Choice::parse(None), Ok(Choice::Auto));
        assert_eq!(Choice::parse(Some("")), Ok(Choice::Auto));
        assert_eq!(Choice::parse(Some(" Auto ")), Ok(Choice::Auto));
        assert_eq!(Choice::parse(Some("WGPU")), Ok(Choice::Wgpu));
        assert_eq!(Choice::parse(Some("glow")), Ok(Choice::Glow));
        let why = Choice::parse(Some("vulkan")).unwrap_err();
        assert!(why.contains("PORTAGEHUB_RENDERER=vulkan") && why.contains("auto, wgpu, glow"), "{why}");
    }

    #[test]
    fn auto_starts_with_wgpu_and_only_auto_falls_back() {
        assert!(matches!(Choice::Auto.first(), eframe::Renderer::Wgpu));
        assert!(matches!(Choice::Wgpu.first(), eframe::Renderer::Wgpu));
        assert!(matches!(Choice::Glow.first(), eframe::Renderer::Glow));
        assert!(Choice::Auto.falls_back(true));
        assert!(!Choice::Auto.falls_back(false), "no display: neither renderer can open a window");
        assert!(!Choice::Wgpu.falls_back(true) && !Choice::Glow.falls_back(true), "a pinned renderer never falls back");
    }
}
