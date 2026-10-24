//! Parser for PMS dependency-style strings: DEPEND, LICENSE, REQUIRED_USE,
//! SRC_URI, RESTRICT … (`flag? ( … )`, `|| ( … )`, `^^ ( … )`, `?? ( … )`).

use super::atom::Atom;
use std::collections::HashSet;

// Leaves (with their parsed atom) are the common case; boxing them would cost
// an allocation per token.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone)]
pub enum DepNode {
    /// A leaf token; `atom` is filled in when the token parses as an atom.
    Leaf {
        text: String,
        atom: Option<Atom>,
    },
    UseCond {
        flag: String,
        negate: bool,
        children: Vec<DepNode>,
    },
    AnyOf(Vec<DepNode>),
    ExactlyOne(Vec<DepNode>),
    AtMostOne(Vec<DepNode>),
    All(Vec<DepNode>),
}

pub fn parse(s: &str) -> Vec<DepNode> {
    let tokens: Vec<&str> = s.split_whitespace().collect();
    let mut pos = 0;
    parse_list(&tokens, &mut pos)
}

fn parse_group(tokens: &[&str], pos: &mut usize) -> Vec<DepNode> {
    if tokens.get(*pos) == Some(&"(") {
        *pos += 1;
        let children = parse_list(tokens, pos);
        if tokens.get(*pos) == Some(&")") {
            *pos += 1;
        }
        children
    } else {
        Vec::new()
    }
}

fn parse_list(tokens: &[&str], pos: &mut usize) -> Vec<DepNode> {
    let mut out = Vec::new();
    while let Some(&tok) = tokens.get(*pos) {
        match tok {
            ")" => break,
            "(" => out.push(DepNode::All(parse_group(tokens, pos))),
            "||" => {
                *pos += 1;
                out.push(DepNode::AnyOf(parse_group(tokens, pos)));
            }
            "^^" => {
                *pos += 1;
                out.push(DepNode::ExactlyOne(parse_group(tokens, pos)));
            }
            "??" => {
                *pos += 1;
                out.push(DepNode::AtMostOne(parse_group(tokens, pos)));
            }
            t if t.ends_with('?') && tokens.get(*pos + 1) == Some(&"(") => {
                *pos += 1;
                let name = &t[..t.len() - 1];
                let (negate, flag) = match name.strip_prefix('!') {
                    Some(f) => (true, f.to_string()),
                    None => (false, name.to_string()),
                };
                let children = parse_group(tokens, pos);
                out.push(DepNode::UseCond { flag, negate, children });
            }
            t => {
                *pos += 1;
                out.push(DepNode::Leaf { text: t.to_string(), atom: Atom::parse(t) });
            }
        }
    }
    out
}

/// Collect every atom in the tree, optionally only those whose USE
/// conditionals are satisfied by `enabled` (all `||` branches are kept).
pub fn collect_atoms<'a>(nodes: &'a [DepNode], enabled: Option<&HashSet<String>>, out: &mut Vec<&'a Atom>) {
    for n in nodes {
        match n {
            DepNode::Leaf { atom: Some(a), .. } => out.push(a),
            DepNode::Leaf { .. } => {}
            DepNode::UseCond { flag, negate, children } => {
                let active = match enabled {
                    Some(set) => set.contains(flag) != *negate,
                    None => true,
                };
                if active {
                    collect_atoms(children, enabled, out);
                }
            }
            DepNode::AnyOf(c) | DepNode::ExactlyOne(c) | DepNode::AtMostOne(c) | DepNode::All(c) => collect_atoms(c, enabled, out),
        }
    }
}

/// Atoms that must hold for an installed package's (already USE-reduced)
/// dependencies: `||`, `^^` and `??` groups are skipped because another
/// alternative may satisfy them.
pub fn collect_required_atoms<'a>(nodes: &'a [DepNode], out: &mut Vec<&'a Atom>) {
    for n in nodes {
        match n {
            DepNode::Leaf { atom: Some(a), .. } => out.push(a),
            DepNode::Leaf { .. } => {}
            DepNode::UseCond { children, .. } | DepNode::All(children) => collect_required_atoms(children, out),
            DepNode::AnyOf(_) | DepNode::ExactlyOne(_) | DepNode::AtMostOne(_) => {}
        }
    }
}

/// Leaf texts (e.g. license names) reachable under the given USE flags.
pub fn collect_leaves(nodes: &[DepNode], enabled: Option<&HashSet<String>>, out: &mut Vec<String>) {
    for n in nodes {
        match n {
            DepNode::Leaf { text, .. } => {
                if text != "->" && !out.contains(text) {
                    out.push(text.clone())
                }
            }
            DepNode::UseCond { flag, negate, children } => {
                let active = enabled.is_none_or(|set| set.contains(flag) != *negate);
                if active {
                    collect_leaves(children, enabled, out);
                }
            }
            DepNode::AnyOf(c) | DepNode::ExactlyOne(c) | DepNode::AtMostOne(c) | DepNode::All(c) => collect_leaves(c, enabled, out),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nested() {
        let t = parse("a/b x? ( c/d !y? ( e/f ) ) || ( g/h i/j ) ^^ ( k l )");
        assert_eq!(t.len(), 4);
        let mut atoms = Vec::new();
        collect_atoms(&t, None, &mut atoms);
        assert_eq!(atoms.len(), 5);
        let mut on = HashSet::new();
        on.insert("y".to_string());
        let mut atoms = Vec::new();
        collect_atoms(&t, Some(&on), &mut atoms);
        // x is off so c/d and e/f are dropped
        assert_eq!(atoms.iter().map(|a| a.key()).collect::<Vec<_>>(), vec!["a/b", "g/h", "i/j"]);
    }
}
