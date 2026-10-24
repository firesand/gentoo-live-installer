//! Emerge: recipes, the full option builder, the preview/conflict resolver
//! and live progress of running jobs.

use super::common::{self, page, subnav};
use crate::app::{Action, App, Job, JobKind, Tab};
use crate::data::Reload;
use crate::edits;
use crate::exec::Priv;
use crate::portage::config::ConfKind;
use crate::portage::emerge::{self, Action as EAction, EmergeOptions, Group, Kind, SPECS, Value};
use crate::portage::history::fmt_duration;
use crate::portage::pretend::{EntryType, Plan, Problem};
use crate::portage::progress::Stage;
use crate::ui::theme::{self, *};
use crate::ui::widgets as w;
use egui::{Color32, Margin, RichText, Ui};
use egui_phosphor::regular as icon;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Page {
    #[default]
    Recipes,
    Custom,
    Review,
    Activity,
}

#[derive(Default)]
pub struct State {
    pub page: Page,
    pub options: EmergeOptions,
    pub pretend_job: Option<u64>,
    pub plan: Option<Plan>,
    pub plan_opts: Option<EmergeOptions>,
    pub active_job: Option<u64>,
    pub selected_job: Option<u64>,
    pub target_input: String,
    pub log_filter: String,
    pub log_errors_only: bool,
    pub follow: bool,
    pub show_raw: bool,
    pub confirm_dangerous: bool,
    /// The lines of the shown job that pass the output filter, kept between frames.
    pub log_cache: Option<LogCache>,
    pub env_features: String,
    pub env_makeopts: String,
    /// Start a kernel emerge even though a boot partition is read-only.
    pub boot_override: bool,
}

pub fn show(app: &mut App, ui: &mut Ui) {
    let mut p = app.emerge.page;
    let running = app.running_count();
    let activity_label = if running > 0 { format!("Activity ({running})") } else { "Activity".to_string() };
    subnav(ui, &mut p, &[(Page::Recipes, "Quick actions"), (Page::Custom, "All options"), (Page::Review, "Preview & conflicts"), (Page::Activity, activity_label.as_str())], |ui| {
        if let Some(env) = &app.data.env {
            let d = env.get("EMERGE_DEFAULT_OPTS");
            if !d.is_empty() {
                ui.label(RichText::new(format!("EMERGE_DEFAULT_OPTS: {d}")).size(11.5).color(TEXT_FAINT)).on_hover_text("From make.conf; added to every emerge run unless you enable “Ignore EMERGE_DEFAULT_OPTS”.");
            }
        }
    });
    app.emerge.page = p;
    match app.emerge.page {
        Page::Recipes => recipes(app, ui),
        Page::Custom => custom(app, ui),
        Page::Review => review(app, ui),
        Page::Activity => activity(app, ui),
    }
}

fn recipes(app: &mut App, ui: &mut Ui) {
    page(ui, "recipes", |ui| {
        w::title(ui, "What do you want to do?");
        w::subtitle(ui, "Each action first shows a preview of exactly what emerge will do. Nothing changes until you press START.");
        ui.add_space(12.0);
        let list = emerge::recipes();
        let gap = 10.0;
        let per_row = ((ui.available_width() + gap) / (340.0 + gap)).floor().max(1.0) as usize;
        let tile_w = (ui.available_width() - gap * (per_row as f32 - 1.0)) / per_row as f32;
        for row in list.chunks(per_row) {
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = gap;
                for r in row {
                    let resp = w::card_tinted(if r.recommended { GREEN } else { ACCENT_DIM })
                        .show(ui, |ui| {
                            ui.set_width(tile_w - 26.0);
                            ui.set_height(70.0);
                            ui.horizontal(|ui| {
                                ui.label(RichText::new(r.icon).size(30.0).color(if r.recommended { GREEN } else { ACCENT }));
                                ui.vertical(|ui| {
                                    ui.horizontal(|ui| {
                                        ui.label(RichText::new(r.title).size(16.0).family(theme::bold()).color(WHITE));
                                        if r.recommended {
                                            w::badge(ui, "RECOMMENDED", GREEN_TEXT, GREEN_DARK);
                                        }
                                    });
                                    ui.add(egui::Label::new(RichText::new(r.subtitle).color(TEXT_DIM)).wrap());
                                });
                            });
                        })
                        .response
                        .interact(egui::Sense::click())
                        .on_hover_cursor(egui::CursorIcon::PointingHand);
                    if resp.clicked() {
                        app.push(Action::Pretend((r.build)()));
                    }
                }
            });
            ui.add_space(8.0);
        }
        w::section(ui, "Repositories");
        ui.horizontal(|ui| {
            if w::big_button(ui, &format!("{}  SYNC ALL", icon::ARROWS_CLOCKWISE), GRAD_BLUE, 180.0).clicked() {
                app.push(Action::Emerge(EmergeOptions { action: EAction::Sync, ..Default::default() }, vec![]));
            }
            if w::big_button(ui, &format!("{}  WEB SYNC (snapshot)", icon::CLOUD_ARROW_DOWN), GRAD_GREY, 200.0).on_hover_text("emerge-webrsync: download a signed snapshot (useful behind firewalls)").clicked() {
                app.push(Action::Root { title: "emerge-webrsync".into(), argv: vec!["emerge-webrsync".into()], reload: vec![Reload::Repos], kind: JobKind::Sync });
                app.emerge.page = Page::Activity;
            }
            if w::big_button(ui, &format!("{}  OVERLAYS…", icon::GIT_BRANCH), GRAD_GREY, 160.0).clicked() {
                app.push(Action::Tab(Tab::Overlays));
            }
        });
        w::section(ui, "Maintenance");
        ui.horizontal_wrapped(|ui| {
            ui.set_row_height(64.0);
            let tools: [(&str, &str, &str, Vec<&str>); 6] = [
                (icon::NEWSPAPER, "Read news", "eselect news read new", vec!["eselect", "news", "read", "new"]),
                (icon::SHIELD_CHECK, "Security check (GLSA)", "glsa-check -l affected", vec!["glsa-check", "-l", "affected"]),
                (icon::BROOM, "Clean old sources", "eclean-dist --deep", vec!["eclean-dist", "--deep"]),
                (icon::GEAR, "Update environment", "env-update", vec!["env-update"]),
                (icon::INFO, "emerge --info", "System report for bug reports", vec!["emerge", "--info"]),
                (icon::DATABASE, "Regenerate overlay caches", "emerge --regen", vec!["emerge", "--regen"]),
            ];
            for (ic, title, sub, argv) in tools {
                let available = crate::exec_available(argv[0]);
                let r = ui
                    .add_enabled_ui(available, |ui| {
                        w::card().show(ui, |ui| {
                            ui.set_width(250.0);
                            ui.set_height(40.0);
                            ui.horizontal(|ui| {
                                ui.label(RichText::new(ic).size(22.0).color(ACCENT));
                                ui.vertical(|ui| {
                                    ui.label(w::strong(title));
                                    ui.label(RichText::new(if available { sub.to_string() } else { format!("{} not installed", argv[0]) }).size(12.0).color(TEXT_DIM));
                                });
                            });
                        })
                    })
                    .inner
                    .response
                    .interact(egui::Sense::click());
                if available && r.clicked() {
                    app.push(Action::Root { title: title.to_string(), argv: argv.iter().map(|s| s.to_string()).collect(), reload: vec![Reload::News], kind: JobKind::Generic });
                    app.emerge.page = Page::Activity;
                }
            }
        });
    });
}

fn custom(app: &mut App, ui: &mut Ui) {
    let show_adv = app.settings.show_advanced;
    page(ui, "custom", |ui| {
        w::title(ui, "Build an emerge command");
        w::subtitle(ui, "Every emerge option as a switch. Hover the ? for an explanation. The equivalent command line is shown at the bottom.");
        ui.add_space(8.0);
        // Action
        w::section(ui, "Action");
        ui.horizontal_wrapped(|ui| {
            for a in EAction::ALL {
                if matches!(a, EAction::Regen | EAction::Metadata | EAction::Config) && !show_adv {
                    continue;
                }
                let sel = app.emerge.options.action == a;
                let r = w::chip(ui, a.label(), sel).on_hover_text(a.help());
                if r.clicked() {
                    app.emerge.options.action = a;
                }
            }
        });
        ui.label(w::dim(app.emerge.options.action.help()));
        if app.emerge.options.action.dangerous() {
            w::callout(ui, RED, icon::WARNING, "This action can remove packages other software still needs. Prefer Depclean, which checks dependencies.");
        }

        // Targets
        w::section(ui, "Targets (packages and sets)");
        ui.horizontal_wrapped(|ui| {
            let mut remove = None;
            for (i, t) in app.emerge.options.targets.iter().enumerate() {
                if w::chip(ui, &format!("{t}  {}", icon::X), true).on_hover_text("Click to remove").clicked() {
                    remove = Some(i);
                }
            }
            if let Some(i) = remove {
                app.emerge.options.targets.remove(i);
            }
            let r = ui.add(egui::TextEdit::singleline(&mut app.emerge.target_input).hint_text("add package or @set, Enter").desired_width(240.0));
            if r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)) {
                for t in app.emerge.target_input.split_whitespace() {
                    if !app.emerge.options.targets.iter().any(|x| x == t) {
                        app.emerge.options.targets.push(t.to_string());
                    }
                }
                app.emerge.target_input.clear();
                r.request_focus();
            }
        });
        // Suggestions for the typed target
        let q = app.emerge.target_input.trim().to_lowercase();
        if q.len() >= 2
            && !q.starts_with('@')
            && let Some(cat) = app.data.catalog.clone()
        {
            ui.horizontal_wrapped(|ui| {
                let mut n = 0;
                for p in cat.pkgs.iter() {
                    if p.pn.to_lowercase().starts_with(&q) || p.key().starts_with(&q) {
                        if ui.small_button(p.key()).clicked() {
                            app.emerge.options.targets.push(p.key());
                            app.emerge.target_input.clear();
                        }
                        n += 1;
                        if n >= 12 {
                            break;
                        }
                    }
                }
            });
        }
        ui.horizontal_wrapped(|ui| {
            ui.label(w::dim("Sets:"));
            for (s, help) in emerge::COMMON_SETS {
                let has = app.emerge.options.targets.iter().any(|t| t == s);
                if w::chip(ui, s, has).on_hover_text(*help).clicked() {
                    if has {
                        app.emerge.options.targets.retain(|t| t != s);
                    } else {
                        app.emerge.options.targets.push(s.to_string());
                    }
                }
            }
            if let Some(cfg) = &app.data.config
                && let Some(sets) = cfg.set(ConfKind::Sets)
            {
                for f in &sets.files {
                    let s = format!("@{}", f.rel);
                    let has = app.emerge.options.targets.contains(&s);
                    if w::chip(ui, &s, has).on_hover_text("Your set in /etc/portage/sets").clicked() {
                        if has {
                            app.emerge.options.targets.retain(|t| *t != s);
                        } else {
                            app.emerge.options.targets.push(s);
                        }
                    }
                }
            }
        });

        // Options by group
        for g in Group::ALL {
            let specs: Vec<_> = SPECS.iter().filter(|s| s.group == g && (show_adv || !s.advanced)).collect();
            if specs.is_empty() {
                continue;
            }
            let active = specs.iter().filter(|s| app.emerge.options.values.contains_key(s.flag)).count();
            let title = if active > 0 { format!("{}  {}  ({active} set)", g.icon(), g.title()) } else { format!("{}  {}", g.icon(), g.title()) };
            egui::CollapsingHeader::new(RichText::new(title).size(15.0).family(theme::bold()).color(WHITE))
                .id_salt(format!("grp-{g:?}"))
                .default_open(matches!(g, Group::Update | Group::Build))
                .show(ui, |ui| {
                    w::card().show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        egui::Grid::new(format!("opts-{g:?}")).num_columns(3).spacing([16.0, 8.0]).striped(true).show(ui, |ui| {
                            for s in specs {
                                option_row(app, ui, s);
                                ui.end_row();
                            }
                        });
                    });
                });
        }
        if !show_adv {
            ui.horizontal(|ui| {
                ui.label(w::dim("Some expert options are hidden."));
                if ui.link("Show all options").clicked() {
                    app.settings.show_advanced = true;
                }
            });
        }

        // Environment overrides
        egui::CollapsingHeader::new(RichText::new(format!("{}  Environment for this run", icon::SLIDERS)).size(15.0).family(theme::bold()).color(WHITE)).id_salt("env-ovr").show(ui, |ui| {
            w::card().show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.label(w::dim("Temporarily override make.conf variables for this run only."));
                egui::Grid::new("env-grid").num_columns(2).spacing([12.0, 8.0]).show(ui, |ui| {
                    ui.label("MAKEOPTS");
                    ui.add(egui::TextEdit::singleline(&mut app.emerge.env_makeopts).hint_text(app.data.env.as_ref().map(|e| e.get("MAKEOPTS").to_string()).unwrap_or_default()).desired_width(300.0));
                    ui.end_row();
                    ui.label("FEATURES (added)");
                    ui.add(egui::TextEdit::singleline(&mut app.emerge.env_features).hint_text("e.g. -test ccache").desired_width(300.0));
                    ui.end_row();
                    ui.label("Extra arguments");
                    ui.add(egui::TextEdit::singleline(&mut app.emerge.options.extra_args).hint_text("anything not covered above").desired_width(300.0));
                    ui.end_row();
                });
            });
        });
        let mut env = Vec::new();
        if !app.emerge.env_makeopts.trim().is_empty() {
            env.push(("MAKEOPTS".to_string(), app.emerge.env_makeopts.trim().to_string()));
        }
        if !app.emerge.env_features.trim().is_empty() {
            let base = app.data.env.as_ref().map(|e| e.get("FEATURES").to_string()).unwrap_or_default();
            env.push(("FEATURES".to_string(), format!("{base} {}", app.emerge.env_features.trim())));
        }
        app.emerge.options.env = env;

        ui.add_space(10.0);
        w::section(ui, "Command");
        w::command_preview(ui, &app.emerge.options.command_line());
        ui.add_space(8.0);
        let opts = app.emerge.options.clone();
        // The admin helper only runs options and targets it recognises: say so here, not after the click.
        let refused = opts.policy_error();
        if let Some(problem) = &refused {
            w::callout(ui, RED, icon::WARNING, &format!("{problem}. PortageHub only passes options it knows (exactly as spelled) and package atoms or @sets as targets."));
        }
        let needs = (opts.action.needs_targets() && opts.targets.is_empty()) || refused.is_some();
        ui.horizontal(|ui| {
            if opts.action.previewable() {
                if ui.add_enabled_ui(!needs, |ui| w::big_button(ui, &format!("{}  PREVIEW", icon::EYE), GRAD_GREEN, 170.0)).inner.clicked() {
                    app.push(Action::Pretend(opts.clone()));
                }
            } else if ui.add_enabled_ui(!needs, |ui| w::big_button(ui, &format!("{}  RUN", icon::PLAY), GRAD_BLUE, 150.0)).inner.clicked() {
                app.push(Action::Emerge(opts.clone(), vec![]));
            }
            if opts.action.needs_targets() && opts.targets.is_empty() {
                ui.label(RichText::new(format!("{} add at least one target", icon::WARNING)).color(ORANGE));
            }
            if w::button(ui, icon::ARROW_COUNTER_CLOCKWISE, "Reset").clicked() {
                app.emerge.options = EmergeOptions::default();
            }
        });
    });
}

fn option_row(app: &mut App, ui: &mut Ui, s: &emerge::Spec) {
    let cur = app.emerge.options.values.get(s.flag).cloned();
    ui.horizontal(|ui| {
        ui.label(RichText::new(s.label).color(if cur.is_some() { WHITE } else { TEXT }));
        w::help(ui, s.help);
    });
    let flag_txt = match s.short {
        Some(c) => format!("{} / -{c}", s.flag),
        None => s.flag.to_string(),
    };
    ui.label(RichText::new(flag_txt).monospace().size(11.5).color(TEXT_FAINT));
    match s.kind {
        Kind::Switch => {
            let mut on = cur == Some(Value::On);
            if w::toggle(ui, &mut on).changed() {
                if on {
                    app.emerge.options.values.insert(s.flag, Value::On);
                } else {
                    app.emerge.options.values.remove(s.flag);
                }
            }
        }
        Kind::YesNo => {
            #[derive(PartialEq, Clone, Copy)]
            enum Yn {
                D,
                Y,
                N,
            }
            let mut v = match cur {
                Some(Value::Yes) => Yn::Y,
                Some(Value::No) => Yn::N,
                _ => Yn::D,
            };
            if w::segmented(ui, &mut v, &[(Yn::D, "Default"), (Yn::Y, "Yes"), (Yn::N, "No")]).changed() {
                match v {
                    Yn::D => {
                        app.emerge.options.values.remove(s.flag);
                    }
                    Yn::Y => {
                        app.emerge.options.values.insert(s.flag, Value::Yes);
                    }
                    Yn::N => {
                        app.emerge.options.values.insert(s.flag, Value::No);
                    }
                }
            }
        }
        Kind::Number { min, max, allow_plain } => {
            ui.horizontal(|ui| {
                let mut on = matches!(cur, Some(Value::Num(_)));
                if w::toggle(ui, &mut on).changed() {
                    if on {
                        let default = match s.flag {
                            "--backtrack" => Some(30),
                            "--load-average" => Some(std::thread::available_parallelism().map(|n| n.get() as i64).unwrap_or(4)),
                            "--jobs" => Some(2),
                            _ => if allow_plain { None } else { Some(min) },
                        };
                        app.emerge.options.values.insert(s.flag, Value::Num(default));
                    } else {
                        app.emerge.options.values.remove(s.flag);
                    }
                }
                if let Some(Value::Num(n)) = app.emerge.options.values.get_mut(s.flag) {
                    let mut x = n.unwrap_or(0);
                    if ui.add(egui::DragValue::new(&mut x).range(if allow_plain { 0 } else { min }..=max)).changed() {
                        *n = if x == 0 && allow_plain { None } else { Some(x) };
                    }
                    if n.is_none() && allow_plain {
                        ui.label(w::dim("unlimited"));
                    }
                }
            });
        }
        Kind::Atoms => {
            let mut t = match cur {
                Some(Value::Text(t)) => t,
                _ => String::new(),
            };
            if ui.add(egui::TextEdit::singleline(&mut t).hint_text("cat/pkg cat/pkg2").desired_width(260.0)).changed() {
                if t.trim().is_empty() {
                    app.emerge.options.values.remove(s.flag);
                } else {
                    app.emerge.options.values.insert(s.flag, Value::Text(t));
                }
            }
        }
        Kind::Choice(choices) => {
            let mut t = match cur {
                Some(Value::Text(t)) => t,
                _ => String::new(),
            };
            let before = t.clone();
            egui::ComboBox::from_id_salt(s.flag).selected_text(if t.is_empty() { "Default".to_string() } else { t.clone() }).show_ui(ui, |ui| {
                ui.selectable_value(&mut t, String::new(), "Default");
                for c in choices {
                    ui.selectable_value(&mut t, c.to_string(), *c);
                }
            });
            if t != before {
                if t.is_empty() {
                    app.emerge.options.values.remove(s.flag);
                } else {
                    app.emerge.options.values.insert(s.flag, Value::Text(t));
                }
            }
        }
    }
}

fn review(app: &mut App, ui: &mut Ui) {
    let Some(pid) = app.emerge.pretend_job else {
        w::empty_state(ui, icon::EYE, "No preview yet", "Pick a quick action or build a command first.");
        return;
    };
    let job = app.job(pid).cloned();
    page(ui, "review", |ui| {
        let opts = app.emerge.plan_opts.clone().unwrap_or_else(|| app.emerge.options.clone());
        ui.horizontal(|ui| {
            w::title(ui, "Preview");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if w::button(ui, icon::SLIDERS, "Change options").clicked() {
                    app.emerge.options = opts.clone();
                    app.emerge.page = Page::Custom;
                }
                if w::button(ui, icon::ARROWS_CLOCKWISE, "Re-check").clicked() {
                    app.push(Action::RePretend);
                }
            });
        });
        w::command_preview(ui, &opts.command_line());
        ui.add_space(6.0);
        let Some(job) = job else { return };
        if job.running() {
            ui.horizontal(|ui| {
                ui.add(egui::Spinner::new().size(20.0).color(ACCENT));
                ui.label(RichText::new("Calculating dependencies…").size(16.0).color(WHITE));
                ui.label(w::dim(format!("{:.0}s", job.started.elapsed().as_secs_f32())));
                if w::button(ui, icon::STOP, "Cancel").clicked() {
                    app.push(Action::Cancel(job.id));
                }
            });
            if let Some(l) = job.lines.last() {
                ui.label(w::dim(&l.1));
            }
            return;
        }
        let Some(plan) = app.emerge.plan.clone() else {
            ui.label(w::dim("Waiting for results…"));
            return;
        };
        let boot_ok = boot_preflight(app, ui, &plan);
        summary(app, ui, &plan, &opts, boot_ok);
        problems(app, ui, &plan, &opts);
        plan_table(app, ui, &plan);
        if !plan.depclean.is_empty() {
            w::section(ui, &format!("Would be removed ({})", plan.depclean.len()));
            let critical: Vec<&String> = plan
                .depclean
                .iter()
                .filter(|c| {
                    ["sys-kernel/", "sys-devel/gcc-", "sys-devel/binutils-", "sys-libs/glibc-", "dev-lang/python-", "llvm-core/", "sys-devel/llvm-", "sys-apps/portage-", "dev-lang/rust"]
                        .iter()
                        .any(|p| c.starts_with(p))
                })
                .collect();
            if !critical.is_empty() {
                w::callout(
                    ui,
                    ORANGE,
                    icon::WARNING,
                    &format!(
                        "This touches toolchain or kernel packages: {}. That is usually fine (old slots/versions), but make sure you are not running the kernel or compiler being removed (check eselect kernel / gcc-config). Add the ones you want to keep to @world first.",
                        critical.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ")
                    ),
                );
            }
            ui.horizontal_wrapped(|ui| {
                for p in &plan.depclean {
                    let key = crate::portage::atom::split_cpv(p).map(|(c, n, _)| format!("{c}/{n}"));
                    let r = w::badge(ui, p, WHITE, Color32::from_rgb(0x6b, 0x2a, 0x2a)).interact(egui::Sense::click());
                    if r.on_hover_text("Open package (add it to @world there to keep it)").clicked()
                        && let Some(k) = key
                    {
                        app.push(Action::Open(k));
                    }
                }
            });
        }
        ui.add_space(8.0);
        egui::CollapsingHeader::new(RichText::new(format!("{}  Full emerge output", icon::TERMINAL)).color(TEXT)).id_salt("raw-out").show(ui, |ui| {
            let mut cache = None;
            log_view(ui, &job, &mut cache, "", false, false, 400.0);
        });
    });
}

/// Kernel/bootloader packages write to /boot (and often the EFI partition):
/// check that those are mounted read-write. Returns whether emerge may start.
fn boot_preflight(app: &mut App, ui: &mut Ui, plan: &Plan) -> bool {
    use crate::portage::mounts::touches_boot;
    let pkgs: Vec<String> = plan
        .merge_entries()
        .map(|e| e.key.clone())
        .chain(plan.depclean.iter().filter_map(|c| crate::portage::atom::split_cpv(c).map(|(a, b, _)| format!("{a}/{b}"))))
        .filter(|k| touches_boot(k))
        .fold(Vec::new(), |mut v, k| {
            if !v.contains(&k) {
                v.push(k);
            }
            v
        });
    if pkgs.is_empty() {
        return true;
    }
    let mounts = app.boot_mounts();
    let blocked: Vec<_> = mounts.iter().filter(|m| m.blocks_writes()).cloned().collect();
    let pending = app.jobs.iter().any(|j| j.running() && matches!(j.kind, JobKind::Remount));
    let tint = if blocked.is_empty() { GREEN } else { ORANGE };
    w::card_tinted(tint).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            ui.label(RichText::new(icon::HARD_DRIVES).size(22.0).color(tint));
            ui.vertical(|ui| {
                ui.label(RichText::new("Before updating the kernel / bootloader").size(15.5).family(theme::bold()).color(WHITE));
                ui.label(w::dim(format!("{} write to the boot partitions.", pkgs.join(", "))));
            });
        });
        for m in &mounts {
            ui.horizontal(|ui| {
                let (ic, col) = if m.blocks_writes() { (icon::LOCK, ORANGE) } else { (icon::LOCK_OPEN, GREEN_TEXT) };
                ui.label(RichText::new(ic).color(col));
                ui.label(RichText::new(&m.target).monospace().color(WHITE));
                ui.label(w::dim(format!("{} {} · {}{}", m.fstype, m.device, m.state_text(), if m.fstab_ro { " (read-only in fstab)" } else { "" })));
                if m.blocks_writes() && !pending {
                    let restore = m.fstab_ro && app.settings.boot_restore_ro;
                    if w::primary(ui, icon::LOCK_OPEN, if m.mounted { "Make writable" } else { "Mount" })
                        .on_hover_text(format!("mount -o {}{} {}", if m.mounted { "remount," } else { "" }, "rw", m.target))
                        .clicked()
                    {
                        app.remount_boot(&m.target, true, restore);
                    }
                } else if app.remount_restore.contains(&m.target) {
                    ui.label(RichText::new("goes back to read-only after the emerge").size(12.0).color(TEXT_DIM));
                }
            });
        }
        if pending {
            ui.horizontal(|ui| {
                ui.add(egui::Spinner::new().color(ACCENT));
                ui.label(w::dim("Remounting…"));
            });
        }
        if blocked.is_empty() {
            ui.label(RichText::new(format!("{} Boot partitions are writable — ready to install.", icon::CHECK_CIRCLE)).color(GREEN_TEXT));
        } else {
            ui.label(RichText::new(format!(
                "{} {} must be writable, or installkernel/grub cannot update it and the new kernel may not be bootable.",
                icon::WARNING,
                blocked.iter().map(|m| m.target.as_str()).collect::<Vec<_>>().join(", ")
            ))
            .color(ORANGE));
            ui.checkbox(&mut app.emerge.boot_override, "Start anyway (I'll handle it myself)");
        }
        ui.horizontal(|ui| {
            w::toggle(ui, &mut app.settings.boot_restore_ro);
            ui.label(w::dim("Make read-only partitions read-only again when the emerge finishes"));
        });
        ui.horizontal(|ui| {
            w::toggle(ui, &mut app.settings.boot_auto_remount);
            ui.label(w::dim("Always do this automatically for kernel and bootloader updates"));
        });
    });
    ui.add_space(6.0);
    blocked.is_empty() || app.emerge.boot_override || app.settings.boot_auto_remount
}

fn summary(app: &mut App, ui: &mut Ui, plan: &Plan, opts: &EmergeOptions, boot_ok: bool) {
    let merges: Vec<_> = plan.merge_entries().collect();
    let count = |l: &str| merges.iter().filter(|e| e.status_label() == l).count();
    let history = app.data.history.clone().unwrap_or_default();
    let total_est: u64 = merges
        .iter()
        .map(|e| if e.kind == EntryType::Binary { 20 } else { history.estimate_or_default(&e.key).0.as_secs() })
        .sum();
    let unknown = merges.iter().filter(|e| e.kind != EntryType::Binary && history.estimate(&e.key).is_none()).count();
    let blocking = plan.problems.iter().filter(|p| p.is_blocking()).count() + plan.suggestions.len();
    let removing = opts.action == EAction::Depclean || opts.action == EAction::Unmerge;
    let ok = plan.exit_code == Some(0) && blocking == 0;
    w::card_tinted(if ok { GREEN } else if blocking > 0 { ORANGE } else { RED }).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            let (ic, col, head) = if ok {
                (icon::CHECK_CIRCLE, GREEN, if merges.is_empty() && plan.depclean.is_empty() { "Nothing to do".to_string() } else { "Ready to go".to_string() })
            } else if blocking > 0 {
                (icon::WARNING, ORANGE, format!("{blocking} issue(s) need your attention"))
            } else {
                (icon::X_CIRCLE, RED, "emerge reported an error".to_string())
            };
            ui.label(RichText::new(ic).size(34.0).color(col));
            ui.vertical(|ui| {
                ui.label(RichText::new(head).size(20.0).family(theme::bold()).color(WHITE));
                if !plan.total_line.is_empty() {
                    ui.label(w::dim(&plan.total_line));
                }
            });
            ui.add_space(20.0);
            for (l, n) in [("NEW", count("NEW") + count("NEW SLOT")), ("UPDATE", count("UPDATE")), ("REBUILD", count("REBUILD")), ("DOWNGRADE", count("DOWNGRADE"))] {
                if n > 0 {
                    ui.vertical(|ui| {
                        ui.label(RichText::new(n.to_string()).size(22.0).family(theme::bold()).color(WHITE));
                        w::status_badge(ui, l);
                    });
                }
            }
            if !merges.is_empty() {
                ui.add_space(12.0);
                w::stat(ui, "Estimated time", &format!("~{}{}", fmt_duration(std::time::Duration::from_secs(total_est)), if unknown > 0 { "*" } else { "" }));
                if !plan.download_size.is_empty() {
                    ui.add_space(12.0);
                    w::stat(ui, "Download", &plan.download_size);
                }
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let can_run = (ok && (!merges.is_empty() || !plan.depclean.is_empty() || opts.action == EAction::Resume)) || (plan.exit_code == Some(0) && !merges.is_empty());
                let (label, grad) = if removing { (format!("{}  REMOVE", icon::TRASH), GRAD_RED) } else { (format!("{}  START EMERGE", icon::PLAY), GRAD_GREEN) };
                let needs_confirm = removing || opts.action.dangerous();
                let allowed = can_run && (!needs_confirm || app.emerge.confirm_dangerous) && boot_ok;
                if ui.add_enabled_ui(allowed, |ui| w::big_button(ui, &label, grad, 190.0)).inner.clicked() {
                    let mut planned: Vec<(String, String, bool)> = merges.iter().map(|e| (e.cpv.clone(), e.repo.clone(), e.kind == EntryType::Binary)).collect();
                    // Removals are quick; track them like binary merges.
                    planned.extend(plan.depclean.iter().map(|c| (c.clone(), String::new(), true)));
                    app.push(Action::Emerge(opts.clone(), planned));
                    app.emerge.confirm_dangerous = false;
                }
                if needs_confirm && can_run {
                    ui.checkbox(&mut app.emerge.confirm_dangerous, "I reviewed the list");
                }
            });
        });
        if unknown > 0 {
            ui.label(w::dim(format!("* {unknown} package(s) were never built here; their time is guessed from your median build time.")));
        }
    });
    ui.add_space(6.0);
}

fn problems(app: &mut App, ui: &mut Ui, plan: &Plan, opts: &EmergeOptions) {
    let arch = app.data.arch();
    let cfg = app.data.config.clone();
    let target = app.settings.target_file.clone();
    // Autounmask suggestions
    if !plan.suggestions.is_empty() {
        w::section(ui, "Required configuration changes");
        common::tip(
            app,
            ui,
            "emerge needs these /etc/portage changes before it can continue (this is what --autounmask-write would do). Untick anything you don't want, then apply — PortageHub writes the files and re-checks automatically.",
        );
        let mut sel = Vec::new();
        for (i, s) in plan.suggestions.iter().enumerate() {
            w::card().show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    let mut on = app.emerge.plan.as_ref().map(|p| p.suggestions[i].selected).unwrap_or(true);
                    if ui.checkbox(&mut on, "").changed()
                        && let Some(p) = app.emerge.plan.as_mut()
                    {
                        p.suggestions[i].selected = on;
                    }
                    if on {
                        sel.push((s.kind, s.line.clone()));
                    }
                    ui.label(RichText::new(s.kind.icon()).size(18.0).color(ACCENT));
                    ui.vertical(|ui| {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(s.kind.file_name()).size(11.5).family(theme::bold()).color(TEXT_DIM));
                            ui.label(w::mono(&s.line));
                        });
                        let e = crate::portage::config::Entry::new(s.line.split_whitespace().next().unwrap_or(""), s.line.split_whitespace().skip(1).map(String::from).collect());
                        ui.label(w::dim(e.describe(s.kind)));
                        if !s.required_by.is_empty() {
                            ui.label(RichText::new(format!("required by {}", s.required_by.join(" ← "))).size(11.5).color(TEXT_FAINT));
                        }
                        for n in &s.note {
                            ui.label(RichText::new(n).size(11.5).color(TEXT_FAINT));
                        }
                    });
                });
            });
            ui.add_space(2.0);
        }
        ui.horizontal(|ui| {
            if ui.add_enabled_ui(!sel.is_empty(), |ui| w::primary(ui, icon::CHECK, &format!("Apply {} change(s) and re-check", sel.len()))).inner.clicked()
                && let Some(cfg) = &cfg
            {
                let comment = format!("added by PortageHub for: emerge {}", opts.args().join(" "));
                let ch = edits::add_lines(cfg, &sel, &target, Some(&comment));
                let then = if app.settings.auto_repretend { Some(Box::new(Action::RePretend)) } else { None };
                app.push_write("Apply emerge's suggested changes", ch, vec![Reload::Config], then);
            }
            ui.label(w::dim(format!("into …/{target} of each package.* directory")));
        });
    }
    for (pi, p) in plan.problems.iter().enumerate() {
        let (tint, ic) = match p {
            Problem::News { .. } | Problem::PreservedLibs | Problem::ConfigUpdates { .. } => (ACCENT, icon::INFO),
            Problem::Blocker { resolved: true, .. } => (ACCENT, icon::INFO),
            _ => (ORANGE, icon::WARNING),
        };
        ui.add_space(4.0);
        w::card_tinted(tint).show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                ui.label(RichText::new(ic).size(22.0).color(tint));
                ui.label(RichText::new(p.title()).size(15.5).family(theme::bold()).color(WHITE));
            });
            problem_body(app, ui, p, pi, opts, &arch);
        });
    }
    if plan.problems.is_empty() && plan.suggestions.is_empty() && plan.exit_code.is_some_and(|c| c != 0) {
        w::callout(ui, RED, icon::X_CIRCLE, "emerge exited with an error. Check the full output below.");
    }
}

fn retry_with(app: &mut App, opts: &EmergeOptions, f: impl FnOnce(&mut EmergeOptions)) {
    let mut o = opts.clone();
    f(&mut o);
    app.push(Action::Pretend(o));
}

fn problem_body(app: &mut App, ui: &mut Ui, p: &Problem, pi: usize, opts: &EmergeOptions, arch: &str) {
    let cfg = app.data.config.clone();
    let target = app.settings.target_file.clone();
    match p {
        Problem::Masked { atom, candidates, comments } => {
            ui.label(w::dim(format!("Every version that could satisfy {atom} is masked. Pick one and PortageHub will add the needed entry:")));
            for c in candidates {
                ui.horizontal(|ui| {
                    ui.label(w::mono(&c.cpv));
                    if !c.repo.is_empty() {
                        ui.label(w::dim(format!("::{}", c.repo)));
                    }
                    ui.label(RichText::new(format!("masked by: {}", c.reasons)).color(ORANGE));
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        let fixes = c.fixes(arch);
                        if fixes.is_empty() {
                            ui.label(w::dim("cannot be fixed by configuration (e.g. EAPI too new: update Portage)"));
                        } else if w::primary(ui, icon::LOCK_OPEN, "Allow this version").on_hover_text(fixes.iter().map(|f| format!("{}: {}", f.0.file_name(), f.1)).collect::<Vec<_>>().join("\n")).clicked()
                            && let Some(cfg) = &cfg
                        {
                            let ch = edits::add_lines(cfg, &fixes, &target, Some(&format!("unmasked by PortageHub for {atom}")));
                            app.push_write(format!("Allow {}", c.cpv), ch, vec![Reload::Config], Some(Box::new(Action::RePretend)));
                        }
                    });
                });
            }
            if !comments.is_empty() {
                egui::CollapsingHeader::new("Mask reason").id_salt(("mask-reason", pi)).show(ui, |ui| {
                    for l in comments {
                        ui.label(w::mono(l));
                    }
                });
            }
        }
        Problem::NoEbuilds { atom } => {
            ui.label(w::dim("Check the spelling, sync your repositories, or add the overlay that provides it."));
            ui.horizontal(|ui| {
                let name = atom.rsplit('/').next().unwrap_or(atom).to_string();
                if w::button(ui, icon::MAGNIFYING_GLASS, &format!("Search for “{name}”")).clicked() {
                    app.store.in_desc = true;
                    app.push(Action::Search(name.clone()));
                }
                if w::button(ui, icon::GIT_BRANCH, "Find an overlay").clicked() {
                    app.overlays.search = name.clone();
                    app.overlays.page = super::overlays::Page::Add;
                    app.push(Action::Reload(Reload::RemoteRepos));
                    app.push(Action::Tab(Tab::Overlays));
                }
                if w::button(ui, icon::ARROWS_CLOCKWISE, "Sync repositories").clicked() {
                    app.push(Action::Emerge(EmergeOptions { action: EAction::Sync, ..Default::default() }, vec![]));
                }
            });
        }
        Problem::Ambiguous { name, options } => {
            ui.label(w::dim("Choose which one you meant:"));
            ui.horizontal_wrapped(|ui| {
                for o in options {
                    if w::primary(ui, icon::PACKAGE, o).clicked() {
                        retry_with(app, opts, |x| {
                            for t in &mut x.targets {
                                if t == name || t.ends_with(&format!("/{name}")) || t.trim_start_matches(['=', '<', '>', '~']).starts_with(name.as_str()) {
                                    *t = o.clone();
                                }
                            }
                        });
                    }
                }
            });
        }
        Problem::SlotConflict(c) => {
            ui.label(w::dim("Two different versions of the same slot are required at the same time. Usually one of the packages pulling them in needs an update or rebuild too."));
            for (cpv, how) in &c.candidates {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(icon::DOT_OUTLINE).color(ORANGE));
                    ui.label(w::mono(cpv));
                    ui.label(w::dim(how));
                });
            }
            if !c.pulled_by.is_empty() {
                ui.label(RichText::new("Pulled in by:").color(TEXT_DIM));
                ui.horizontal_wrapped(|ui| {
                    for pb in &c.pulled_by {
                        let key = pb.split_whitespace().next().and_then(crate::portage::atom::split_cpv).map(|(a, b, _)| format!("{a}/{b}"));
                        if ui.small_button(pb).clicked()
                            && let Some(k) = key
                        {
                            app.push(Action::Open(k));
                        }
                    }
                });
            }
            ui.label(RichText::new("Try one of these:").color(WHITE));
            ui.horizontal_wrapped(|ui| {
                if w::primary(ui, icon::ARROWS_SPLIT, "Retry with more backtracking (50)").clicked() {
                    retry_with(app, opts, |o| {
                        o.values.insert("--backtrack", Value::Num(Some(50)));
                    });
                }
                if w::button(ui, icon::GLOBE, "Update the whole world together").on_hover_text("emerge -uDN @world plus your targets, so dependents are updated at the same time").clicked() {
                    retry_with(app, opts, |o| {
                        o.values.insert("--update", Value::On);
                        o.values.insert("--deep", Value::On);
                        o.values.insert("--newuse", Value::On);
                        if !o.targets.iter().any(|t| t == "@world") {
                            o.targets.push("@world".into());
                        }
                    });
                }
                if w::button(ui, icon::LIST_MAGNIFYING_GLASS, "Show verbose conflict info").clicked() {
                    retry_with(app, opts, |o| {
                        o.values.insert("--verbose-conflicts", Value::On);
                    });
                }
                if w::button(ui, icon::LOCK_OPEN, "Backtrack with autounmask").clicked() {
                    retry_with(app, opts, |o| {
                        o.values.insert("--autounmask-backtrack", Value::Yes);
                    });
                }
            });
            egui::CollapsingHeader::new("Details from emerge").id_salt(("slot-raw", pi)).show(ui, |ui| {
                for l in &c.raw {
                    ui.label(w::mono(l));
                }
            });
        }
        Problem::Blocker { blocker, blocked, hard, resolved } => {
            if *resolved {
                ui.label(w::dim(format!("{blocker} will be removed automatically when {blocked} is installed (soft blocker, lowercase b).")));
            } else {
                ui.label(w::dim(format!(
                    "{blocked} cannot be installed while {blocker} is present{}. Remove the blocker first, then retry.",
                    if *hard { " (hard blocker)" } else { "" }
                )));
                ui.horizontal(|ui| {
                    let key = crate::portage::atom::Atom::parse(blocker).map(|a| a.key()).unwrap_or(blocker.clone());
                    if w::danger(ui, icon::TRASH, &format!("Preview removing {key}")).clicked() {
                        app.push(Action::Pretend(EmergeOptions { action: EAction::Unmerge, ..Default::default() }.targets(&[key.as_str()])));
                    }
                    if w::button(ui, icon::INFO, "Open blocker").clicked() {
                        app.push(Action::Open(key));
                    }
                });
            }
        }
        Problem::RequiredUse { pkg, constraint, use_line } => {
            ui.label(w::dim("The package's REQUIRED_USE rules are not satisfied by the current flags:"));
            for c in constraint {
                ui.label(w::mono(c));
            }
            if !use_line.is_empty() {
                ui.label(RichText::new(use_line).monospace().size(11.5).color(TEXT_FAINT));
            }
            let key = crate::portage::atom::split_cpv(pkg.split("::").next().unwrap_or(pkg)).map(|(c, p, _)| format!("{c}/{p}"));
            if let Some(k) = key
                && w::primary(ui, icon::TOGGLE_RIGHT, "Open its USE flags").clicked()
            {
                app.push(Action::Open(k));
                app.detail.tab = super::details::DTab::Use;
            }
        }
        Problem::Circular { lines, use_fixes } => {
            if use_fixes.is_empty() {
                ui.label(w::dim("emerge could not suggest a fix. Often installing one of the packages with fewer USE flags first breaks the cycle."));
            } else {
                ui.label(w::dim("emerge suggests changing these USE flags temporarily:"));
                for (pkg, change) in use_fixes {
                    ui.horizontal(|ui| {
                        ui.label(w::mono(format!("{pkg}: {change}")));
                        if w::primary(ui, icon::CHECK, "Apply").clicked()
                            && let Some(cfg) = &cfg
                        {
                            let atom = crate::portage::atom::Atom::parse(pkg).map(|a| a.key()).unwrap_or(pkg.clone());
                            let line = format!("{atom} {}", change.replace(',', " "));
                            let ch = edits::add_lines(cfg, &[(ConfKind::Use, line)], &target, Some("temporary: break circular dependency"));
                            app.push_write("Break dependency cycle", ch, vec![Reload::Config], Some(Box::new(Action::RePretend)));
                        }
                    });
                }
            }
            egui::CollapsingHeader::new("Cycle details").id_salt(("cycle", pi)).show(ui, |ui| {
                for l in lines {
                    ui.label(w::mono(l));
                }
            });
        }
        Problem::Unsatisfied { lines } | Problem::Generic { lines, .. } => {
            for l in lines.iter().take(30) {
                ui.label(w::mono(l));
            }
            ui.horizontal(|ui| {
                if w::button(ui, icon::ARROWS_SPLIT, "Retry with --backtrack=50").clicked() {
                    retry_with(app, opts, |o| {
                        o.values.insert("--backtrack", Value::Num(Some(50)));
                    });
                }
                if w::button(ui, icon::LIST_MAGNIFYING_GLASS, "Retry verbose (--tree --verbose-conflicts)").clicked() {
                    retry_with(app, opts, |o| {
                        o.values.insert("--tree", Value::On);
                        o.values.insert("--verbose-conflicts", Value::On);
                    });
                }
            });
        }
        Problem::Skipped { slot_key, candidate, required } => {
            ui.label(w::dim(format!(
                "The rest of the update still runs, but {slot_key} stays at the installed version because something requires exactly that version:"
            )));
            for (atom, by) in required {
                ui.horizontal(|ui| {
                    ui.label(w::mono(atom));
                    ui.label(w::dim("required by"));
                    ui.label(w::strong(by));
                    if let Some(set) = by.strip_prefix('@') {
                        let path = std::path::PathBuf::from(format!("/etc/portage/sets/{set}"));
                        if path.exists() && w::primary(ui, icon::PENCIL_SIMPLE, &format!("Edit set {by}")).on_hover_text(format!("Change or remove {atom} to allow {candidate}")).clicked() {
                            app.config_view.page = super::config::Page::Kind(ConfKind::Sets);
                            app.config_view.file_filter = Some(path);
                            app.push(Action::Tab(Tab::Config));
                        }
                    } else if let Some(k) = by.trim_start_matches('(').split([',', ' ']).next().and_then(crate::portage::atom::split_cpv).map(|(c, p, _)| format!("{c}/{p}"))
                        && w::button(ui, icon::INFO, "Open package").clicked()
                    {
                        app.push(Action::Open(k));
                    }
                });
            }
            if required.iter().any(|(_, by)| by.starts_with('@')) {
                ui.label(w::dim("Your own set pins this version. Edit the set if you want the new one; otherwise this is expected."));
            }
        }
        Problem::PreservedLibs => {
            ui.label(w::dim("Old library versions are kept because installed programs still use them. Rebuild those programs to finish the update."));
            if w::primary(ui, icon::WRENCH, "Preview emerge @preserved-rebuild").clicked() {
                app.push(Action::Pretend(EmergeOptions::default().targets(&["@preserved-rebuild"])));
            }
        }
        Problem::News { .. } => {
            ui.label(w::dim("Gentoo news can contain important upgrade instructions. Read them before big updates."));
            if w::primary(ui, icon::NEWSPAPER, "Open news").clicked() {
                app.config_view.page = super::config::Page::News;
                app.push(Action::Tab(Tab::Config));
            }
        }
        Problem::ConfigUpdates { .. } => {
            ui.label(w::dim("Updated packages installed new default configuration files that need merging."));
            if w::primary(ui, icon::FILE_ARROW_UP, "Review config updates").clicked() {
                app.config_view.page = super::config::Page::Updates;
                app.push(Action::Tab(Tab::Config));
            }
        }
    }
}

fn plan_table(app: &mut App, ui: &mut Ui, plan: &Plan) {
    if plan.entries.is_empty() {
        return;
    }
    let history = app.data.history.clone().unwrap_or_default();
    let order = if plan.reverse { "tree view, dependencies below their parents" } else { "in build order" };
    w::section(ui, &format!("Merge list ({} entries, {order})", plan.merge_entries().count()));
    egui::Grid::new("plan-grid").num_columns(6).striped(true).spacing([14.0, 6.0]).min_col_width(30.0).show(ui, |ui| {
        ui.label(w::dim("#"));
        ui.label(w::dim("Status"));
        ui.label(w::dim("Package"));
        ui.label(w::dim("Change"));
        ui.label(w::dim("Est. time"));
        ui.label(w::dim("USE flags"));
        ui.end_row();
        let mut n = 0;
        for e in &plan.entries {
            if matches!(e.kind, EntryType::Ebuild | EntryType::Binary) {
                n += 1;
                ui.label(w::dim(n.to_string()));
            } else {
                ui.label("");
            }
            ui.horizontal(|ui| {
                w::status_badge(ui, e.status_label());
                if e.kind == EntryType::Binary {
                    w::badge(ui, "BIN", WHITE, Color32::from_rgb(0x2a, 0x6f, 0x97)).on_hover_text("Installed from a binary package (fast)");
                }
                if e.has('F') || e.has('f') {
                    w::badge(ui, "FETCH", WHITE, ORANGE).on_hover_text("Fetch-restricted: download the file manually");
                }
                if e.has('I') {
                    w::badge(ui, "INTERACTIVE", WHITE, PURPLE);
                }
            });
            ui.horizontal(|ui| {
                ui.add_space(e.depth as f32 * 4.0);
                let r = ui.add(egui::Label::new(RichText::new(&e.key).color(WHITE)).sense(egui::Sense::click()));
                if r.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() && !e.key.is_empty() {
                    app.push(Action::Open(e.key.clone()));
                }
                if !e.repo.is_empty() {
                    ui.label(w::dim(format!("::{}", e.repo)));
                }
            });
            let change = if e.old.is_empty() { e.version.clone() } else { format!("{} → {}", e.old.join(", ").split("::").next().unwrap_or(""), e.version) };
            ui.label(RichText::new(change).color(TEXT));
            if matches!(e.kind, EntryType::Ebuild | EntryType::Binary) {
                let (d, known) = if e.kind == EntryType::Binary { (std::time::Duration::from_secs(20), true) } else { history.estimate_or_default(&e.key) };
                ui.label(RichText::new(format!("{}{}", fmt_duration(d), if known { "" } else { "?" })).color(if known { TEXT } else { TEXT_FAINT }));
            } else {
                ui.label("");
            }
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing = egui::vec2(3.0, 3.0);
                let flags = e.use_changes();
                let changed: Vec<_> = flags.iter().filter(|f| f.2).collect();
                for (f, on, ch) in changed.iter().take(10) {
                    w::flag_chip(ui, f, *on, *ch);
                }
                let rest = flags.len() - changed.len().min(10);
                if rest > 0 {
                    ui.label(w::dim(format!("+{rest}"))).on_hover_text(e.use_vars.iter().map(|(k, v)| format!("{k}=\"{v}\"")).collect::<Vec<_>>().join("\n"));
                }
            });
            ui.end_row();
        }
    });
    ui.label(w::dim("Flags marked * changed since the last build; % means the flag is new to the ebuild."));
}

fn activity(app: &mut App, ui: &mut Ui) {
    // The sidebar needs a title and a status per job, not their whole logs.
    let jobs: Vec<JobRow> = app.jobs.iter().filter(|j| !matches!(j.kind, JobKind::Capture { .. }) || app.settings.show_advanced).map(JobRow::of).collect();
    let batches: Vec<(String, usize, usize)> = app.batches.iter().map(|b| (b.title.clone(), b.progress().0, b.progress().1)).collect();
    egui::Panel::left("jobs-side").resizable(true).default_size(300.0).frame(egui::Frame::new().fill(SIDEBAR).inner_margin(Margin::symmetric(0, 8))).show(ui, |ui| {
        ui.horizontal(|ui| {
            ui.add_space(12.0);
            ui.label(RichText::new("JOBS").size(12.0).family(theme::bold()).color(TEXT_DIM));
        });
        for (t, done, total) in &batches {
            ui.horizontal(|ui| {
                ui.add_space(12.0);
                ui.add(egui::Spinner::new().size(12.0).color(ACCENT));
                ui.label(RichText::new(format!("{t} ({done}/{total})")).size(12.5).color(TEXT));
            });
        }
        egui::ScrollArea::vertical().id_salt("jobs-list").show(ui, |ui| {
            for j in jobs.iter().rev() {
                let ic = if j.running {
                    icon::CIRCLE_NOTCH
                } else if j.success {
                    icon::CHECK_CIRCLE
                } else {
                    icon::X_CIRCLE
                };
                let sel = app.emerge.selected_job.or(app.emerge.active_job) == Some(j.id);
                let r = w::side_item(ui, ic, &j.title, sel, None).on_hover_text(format!("{}\n{}", j.command, j.status));
                if r.clicked() {
                    app.emerge.selected_job = Some(j.id);
                }
            }
        });
        if jobs.is_empty() {
            ui.add_space(10.0);
            ui.horizontal(|ui| {
                ui.add_space(12.0);
                ui.label(w::dim("Nothing has run yet."));
            });
        }
    });
    egui::CentralPanel::default().frame(egui::Frame::new().fill(BG)).show(ui, |ui| {
        let sel = app.emerge.selected_job.or(app.emerge.active_job).or_else(|| jobs.last().map(|j| j.id));
        let Some(job) = sel.and_then(|id| app.job(id).cloned()) else {
            w::empty_state(ui, icon::TERMINAL_WINDOW, "No activity", "Emerge runs, syncs and other tasks show up here with live progress.");
            return;
        };
        egui::Frame::new().inner_margin(Margin::symmetric(20, 14)).show(ui, |ui| {
            job_header(app, ui, &job);
            if let JobKind::Emerge { tracker, .. } = &job.kind {
                tracker_view(app, ui, tracker);
            }
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                w::section(ui, "Output");
            });
            ui.horizontal(|ui| {
                w::search_box(ui, &mut app.emerge.log_filter, "Filter output", 260.0);
                ui.checkbox(&mut app.emerge.log_errors_only, "Errors & warnings only");
                ui.checkbox(&mut app.settings.log_wrap, "Wrap lines");
                if w::button(ui, icon::COPY, "Copy all").clicked() {
                    ui.ctx().copy_text(job.output());
                }
            });
            let h = ui.available_height() - 8.0;
            let (filter, errors_only) = (app.emerge.log_filter.clone(), app.emerge.log_errors_only);
            log_view(ui, &job, &mut app.emerge.log_cache, &filter, errors_only, app.settings.log_wrap, h.max(160.0));
        });
    });
}

fn job_header(app: &mut App, ui: &mut Ui, job: &Job) {
    ui.horizontal(|ui| {
        let (ic, col) = if job.running() {
            (icon::CIRCLE_NOTCH, ACCENT)
        } else if job.success() {
            (icon::CHECK_CIRCLE, GREEN)
        } else {
            (icon::X_CIRCLE, RED)
        };
        ui.label(RichText::new(ic).size(26.0).color(col));
        ui.vertical(|ui| {
            ui.label(RichText::new(&job.title).size(18.0).family(theme::bold()).color(WHITE));
            let el = job.ended.unwrap_or_else(std::time::Instant::now) - job.started;
            ui.label(w::dim(format!(
                "{} · {} · {}",
                job.status_text(),
                fmt_duration(el),
                if job.privilege == Priv::Root { "as root" } else { "as user" }
            )));
        });
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if job.running() {
                if job.stop.is_some_and(|(s, _)| s == crate::stop::Stage::Killed) {
                    ui.label(w::dim("SIGKILL sent"));
                } else if job.stop.is_some() {
                    ui.label(w::dim("Stopping…"));
                } else {
                    if w::danger(ui, icon::SKULL, "Force stop").on_hover_text("SIGINT now; if it still runs after 10 seconds SIGTERM, after 10 more SIGKILL. Build directories may be left behind.").clicked() {
                        app.push(Action::ForceStop(job.id));
                    }
                    if w::danger(ui, icon::STOP, "Stop").on_hover_text("Send SIGINT (like Ctrl+C). You can resume later with “Resume interrupted emerge”.").clicked() {
                        app.push(Action::Cancel(job.id));
                    }
                }
            } else if matches!(job.kind, JobKind::Emerge { .. }) && !job.success() {
                if w::primary(ui, icon::PLAY, "Resume").on_hover_text("emerge --resume").clicked() {
                    app.push(Action::Pretend(EmergeOptions { action: EAction::Resume, ..Default::default() }));
                }
                if w::button(ui, icon::SKIP_FORWARD, "Resume, skip failed").on_hover_text("emerge --resume --skipfirst").clicked() {
                    app.push(Action::Pretend(EmergeOptions { action: EAction::Resume, ..Default::default() }.with("--skipfirst", Value::On)));
                }
            }
        });
    });
}

fn tracker_view(_app: &mut App, ui: &mut Ui, t: &crate::portage::progress::Tracker) {
    let (done, failed, active) = t.counts();
    let frac = t.fraction();
    ui.add_space(8.0);
    w::card().show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.horizontal(|ui| {
            w::stat(ui, "Progress", &format!("{done} of {} done", t.total.max(t.items.len())));
            ui.add_space(18.0);
            w::stat(ui, "Elapsed", &fmt_duration(t.elapsed()));
            ui.add_space(18.0);
            let rem = if t.finished.is_some() { "—".to_string() } else { format!("~{}", fmt_duration(t.remaining())) };
            w::stat(ui, "Remaining", &rem);
            ui.add_space(18.0);
            w::stat(ui, "Building now", &active.to_string());
            if failed > 0 {
                ui.add_space(18.0);
                ui.vertical(|ui| {
                    ui.label(RichText::new("FAILED").size(11.0).family(theme::bold()).color(RED));
                    ui.label(RichText::new(failed.to_string()).size(14.0).color(RED));
                });
            }
            if !t.jobs_line.is_empty() {
                ui.add_space(18.0);
                ui.label(w::dim(&t.jobs_line));
            }
        });
        ui.add_space(6.0);
        w::progress(ui, frac, 22.0, &format!("{:.0}%  ·  {}", frac * 100.0, t.last_status), t.finished.is_none());
    });
    ui.add_space(6.0);
    let now = t.now();
    egui::ScrollArea::vertical().id_salt("tracker-items").max_height(260.0).show(ui, |ui| {
        egui::Grid::new("tracker-grid").num_columns(5).striped(true).spacing([14.0, 5.0]).show(ui, |ui| {
            for it in &t.items {
                let label = match it.stage {
                    Stage::Queued => "QUEUED",
                    Stage::Fetching | Stage::Building => "BUILDING",
                    Stage::Installing => "INSTALLING",
                    Stage::Done => "DONE",
                    Stage::Failed => "FAILED",
                    Stage::Skipped => "BLOCKED",
                };
                w::status_badge(ui, label).on_hover_text(it.stage.label());
                ui.label(RichText::new(&it.cpv).color(if it.stage.active() { WHITE } else { TEXT }));
                let ph = if it.stage.active() && !it.phase.is_empty() { it.phase.clone() } else { it.stage.label().to_lowercase() };
                ui.label(w::dim(ph));
                let el = it.elapsed(now);
                ui.label(if it.started.is_some() {
                    format!("{} / ~{}{}", fmt_duration(el), fmt_duration(it.estimate), if it.estimate_known { "" } else { "?" })
                } else {
                    format!("~{}{}", fmt_duration(it.estimate), if it.estimate_known { "" } else { "?" })
                });
                ui.allocate_ui(egui::vec2(160.0, 14.0), |ui| {
                    w::progress(ui, it.fraction(now), 10.0, "", false);
                });
                ui.end_row();
            }
        });
    });
}

/// What the Activity sidebar shows of a job.
struct JobRow {
    id: u64,
    title: String,
    command: String,
    status: String,
    running: bool,
    success: bool,
}

impl JobRow {
    fn of(j: &Job) -> JobRow {
        JobRow { id: j.id, title: j.title.clone(), command: j.argv.join(" "), status: j.status_text(), running: j.running(), success: j.success() }
    }
}

/// Does `line` look like an error or a warning (for "Errors & warnings only")?
fn is_problem_line(err: bool, l: &str) -> bool {
    err || l.contains("!!!") || l.contains(" * ERROR") || l.contains("error:") || l.contains("WARNING") || l.trim_start().starts_with("* ")
}

/// Case-insensitive substring test that allocates nothing for ASCII text (`needle` is lower case).
fn icontains(haystack: &str, needle: &str) -> bool {
    if needle.is_empty() {
        return true;
    }
    if haystack.is_ascii() && needle.is_ascii() {
        let (h, n) = (haystack.as_bytes(), needle.as_bytes());
        return h.len() >= n.len() && h.windows(n.len()).any(|w| w.eq_ignore_ascii_case(n));
    }
    haystack.to_lowercase().contains(needle)
}

/// Indices (into `lines`, offset by `base`) of the lines that pass the filter.
fn pass_filter(lines: &[(bool, String)], base: usize, needle_lower: &str, errors_only: bool, out: &mut Vec<u32>) {
    for (i, (err, text)) in lines.iter().enumerate() {
        if icontains(text, needle_lower) && (!errors_only || is_problem_line(*err, text)) {
            out.push((base + i) as u32);
        }
    }
}

/// The filtered view of a job's log, extended line by line as the job prints instead of being
/// recomputed (with every line lower-cased) on every frame.
pub struct LogCache {
    job: u64,
    filter: String,
    errors_only: bool,
    /// Lines of the job seen so far, and how many old ones had been dropped by then.
    seen: usize,
    dropped: usize,
    indices: Vec<u32>,
}

impl LogCache {
    /// Bring the cache up to date with `job` for this filter.
    fn refresh(cache: &mut Option<LogCache>, job: &Job, filter: &str, errors_only: bool) -> usize {
        let needle = filter.to_lowercase();
        let reusable = cache.as_ref().is_some_and(|c| c.job == job.id && c.filter == filter && c.errors_only == errors_only && c.dropped == job.dropped_lines && c.seen <= job.lines.len());
        if !reusable {
            let mut indices = Vec::new();
            pass_filter(&job.lines, 0, &needle, errors_only, &mut indices);
            *cache = Some(LogCache { job: job.id, filter: filter.to_string(), errors_only, seen: job.lines.len(), dropped: job.dropped_lines, indices });
        } else if let Some(c) = cache.as_mut() {
            pass_filter(&job.lines[c.seen..], c.seen, &needle, errors_only, &mut c.indices);
            c.seen = job.lines.len();
        }
        cache.as_ref().map_or(0, |c| c.indices.len())
    }
}

pub fn log_view(ui: &mut Ui, job: &Job, cache: &mut Option<LogCache>, filter: &str, errors_only: bool, wrap: bool, height: f32) {
    let shown = LogCache::refresh(cache, job, filter, errors_only);
    let indices = cache.as_ref().map(|c| c.indices.as_slice()).unwrap_or(&[]);
    egui::Frame::new().fill(Color32::from_rgb(0x0b, 0x10, 0x16)).corner_radius(4).inner_margin(Margin::same(8)).show(ui, |ui| {
        ui.set_width(ui.available_width());
        let row_h = 17.0;
        let total = shown + usize::from(job.partial.is_some());
        egui::ScrollArea::both()
            .id_salt(("log", job.id))
            .max_height(height)
            .auto_shrink([false, false])
            .stick_to_bottom(true)
            .show_rows(ui, row_h, total, |ui, range| {
                for i in range {
                    let (err, text) = if i < shown {
                        let line = &job.lines[indices[i] as usize];
                        (line.0, line.1.as_str())
                    } else {
                        (false, job.partial.as_deref().unwrap_or(""))
                    };
                    let col = line_color(text, err);
                    let rt = RichText::new(text).monospace().size(12.5).color(col);
                    if wrap {
                        ui.add(egui::Label::new(rt).wrap());
                    } else {
                        ui.add(egui::Label::new(rt).extend());
                    }
                }
            });
    });
}

pub fn line_color(l: &str, stderr: bool) -> Color32 {
    let t = l.trim_start();
    if t.starts_with(">>> Completed") || t.starts_with(">>> Emerging") || t.starts_with(">>> Installing") {
        GREEN
    } else if t.starts_with("!!!") || t.contains(" * ERROR") || t.contains("error:") || t.starts_with(">>> Failed") {
        RED
    } else if t.starts_with("* ") || t.contains("WARNING") || t.contains("warning:") {
        YELLOW
    } else if t.starts_with(">>>") || t.starts_with("===") {
        ACCENT
    } else if t.starts_with('[') && (t.starts_with("[ebuild") || t.starts_with("[binary") || t.starts_with("[blocks")) {
        WHITE
    } else if stderr {
        Color32::from_rgb(0xd0, 0xa0, 0x90)
    } else {
        Color32::from_rgb(0xb8, 0xc4, 0xcf)
    }
}

#[cfg(test)]
mod log_tests {
    use super::*;
    use std::sync::Arc;

    fn lines(texts: &[&str]) -> Vec<(bool, String)> {
        texts.iter().map(|t| (false, t.to_string())).collect()
    }

    #[test]
    fn case_insensitive_search_without_allocating() {
        assert!(icontains("Compiling FooBar", "foobar"));
        assert!(icontains("anything", ""));
        assert!(!icontains("short", "much longer"));
        assert!(!icontains("Compiling", "link"));
        // Non-ASCII text still works (and Unicode case folding is what to_lowercase does).
        assert!(icontains("Größe passt", "größe"));
        assert!(icontains("GRÖSSE", "grösse"));
    }

    #[test]
    fn errors_only_keeps_stderr_and_warning_lines() {
        let l = vec![(false, "plain".to_string()), (true, "from stderr".to_string()), (false, " * ERROR: x failed".to_string()), (false, "gcc: warning: y".to_string()), (false, "WARNING z".to_string()), (false, "  * note".to_string()), (false, "!!! boom".to_string())];
        let mut out = Vec::new();
        pass_filter(&l, 0, "", true, &mut out);
        assert_eq!(out, vec![1, 2, 4, 5, 6]);
        let mut all = Vec::new();
        pass_filter(&l, 10, "", false, &mut all);
        assert_eq!(all, (10..17).collect::<Vec<u32>>());
    }

    /// Build a job whose log is `texts` (the rest of the fields do not matter here).
    fn job_with(texts: &[&str]) -> Job {
        Job {
            id: 1,
            title: "t".into(),
            kind: crate::app::JobKind::Generic,
            privilege: crate::exec::Priv::User,
            argv: vec![],
            lines: Arc::new(lines(texts)),
            dropped_lines: 0,
            partial: None,
            started: std::time::Instant::now(),
            ended: None,
            exit: None,
            error: None,
            reload: vec![],
            stop: None,
        }
    }

    #[test]
    fn the_filtered_view_grows_with_the_log_and_is_not_recomputed() {
        let mut job = job_with(&["alpha one", "beta", "ALPHA two"]);
        let mut cache = None;
        assert_eq!(LogCache::refresh(&mut cache, &job, "alpha", false), 2);
        assert_eq!(cache.as_ref().unwrap().indices, vec![0, 2]);
        // New lines are tested alone; the old ones are not looked at again.
        Arc::make_mut(&mut job.lines).extend(lines(&["gamma", "Alpha three"]));
        assert_eq!(LogCache::refresh(&mut cache, &job, "alpha", false), 3);
        assert_eq!(cache.as_ref().unwrap().indices, vec![0, 2, 4]);
        assert_eq!(cache.as_ref().unwrap().seen, 5);
        // Nothing new: the same answer.
        assert_eq!(LogCache::refresh(&mut cache, &job, "alpha", false), 3);
        // A different filter, or errors-only, starts over.
        assert_eq!(LogCache::refresh(&mut cache, &job, "beta", false), 1);
        assert_eq!(cache.as_ref().unwrap().indices, vec![1]);
        assert_eq!(LogCache::refresh(&mut cache, &job, "", true), 0);
    }

    #[test]
    fn dropping_old_lines_invalidates_the_cached_indices() {
        let mut job = job_with(&["x1", "x2", "x3", "x4"]);
        let mut cache = None;
        assert_eq!(LogCache::refresh(&mut cache, &job, "x", false), 4);
        // Two lines fell off the front while many more arrived: the length alone would look "grown".
        Arc::make_mut(&mut job.lines).drain(..2);
        job.dropped_lines += 2;
        Arc::make_mut(&mut job.lines).extend(lines(&["x5", "x6", "x7"]));
        assert_eq!(job.lines.len(), 5);
        assert_eq!(LogCache::refresh(&mut cache, &job, "x", false), 5);
        assert_eq!(cache.as_ref().unwrap().indices, vec![0, 1, 2, 3, 4]);
        // Another job is another log.
        let mut other = job_with(&["x only"]);
        other.id = 9;
        assert_eq!(LogCache::refresh(&mut cache, &other, "x", false), 1);
    }

    /// The case that cost 4-7 ms per frame before: a 60 000-line log and a filter.
    /// Run with `cargo test --release -- --ignored --nocapture log_filter_timing`.
    #[test]
    #[ignore = "timing documentation, not a pass/fail check"]
    fn log_filter_timing() {
        let texts: Vec<String> = (0..60_000).map(|i| format!("make[{}]: Entering directory '/var/tmp/portage/dev-qt/qtbase-6.8.0/work/build/src/plugins/Platforms{i}'", i % 40)).collect();
        let refs: Vec<&str> = texts.iter().map(String::as_str).collect();
        let mut job = job_with(&refs);
        let mut cache = None;
        let t = std::time::Instant::now();
        LogCache::refresh(&mut cache, &job, "PLATFORMS1", false);
        let first = t.elapsed();
        Arc::make_mut(&mut job.lines).push((false, "one more line".into()));
        let t = std::time::Instant::now();
        LogCache::refresh(&mut cache, &job, "PLATFORMS1", false);
        println!("first filter over 60k lines: {first:?}; one new line: {:?}", t.elapsed());
    }

    #[test]
    fn appending_does_not_copy_a_log_nobody_else_holds() {
        let mut job = job_with(&["a", "b"]);
        let before = Arc::as_ptr(&job.lines);
        Arc::make_mut(&mut job.lines).push((false, "c".into()));
        assert_eq!(Arc::as_ptr(&job.lines), before);
        // A view that still holds the old log keeps a consistent snapshot while the job moves on.
        let held = job.lines.clone();
        Arc::make_mut(&mut job.lines).push((false, "d".into()));
        assert_eq!(held.len(), 3);
        assert_eq!(job.lines.len(), 4);
    }
}
