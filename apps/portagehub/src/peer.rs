//! Is the process on the other end of the helper's socket the one that may use it?
//!
//! The helper runs as root through pkexec. Its stdin and stdout are the two ends of the same
//! socketpair PortageHub created (a socket, unlike a pipe, cannot be reopened through
//! `/proc/<pid>/fd` by another process of the same user), and the process at the other end must be
//! the user who authenticated and one of the helper's own ancestors. This is the decision;
//! the helper binary gathers the facts from the kernel and calls it.

/// How many generations of ancestors may sit between the helper and the GUI: pkexec execs the
/// helper in place (the GUI is the parent), sudo and doas add one or two processes.
pub const MAX_ANCESTORS: usize = 4;

/// What the helper knows about one of its standard streams.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamId {
    pub is_socket: bool,
    pub dev: u64,
    pub ino: u64,
}

/// The peer credentials of the socket (`SO_PEERCRED`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCred {
    pub pid: i32,
    pub uid: u32,
}

/// `Ok` when the peer is acceptable. `ancestors` are the helper's parent, grandparent, … (at most
/// [`MAX_ANCESTORS`] are looked at).
pub fn verify_peer(stdin: StreamId, stdout: StreamId, cred: PeerCred, expected_uid: u32, ancestors: &[i32]) -> Result<(), String> {
    if !stdin.is_socket || (stdin.dev, stdin.ino) != (stdout.dev, stdout.ino) {
        return Err("stdin/stdout are not the socket PortageHub provides — the helper must be started by PortageHub".into());
    }
    if cred.uid != expected_uid {
        return Err(format!("peer uid {} is not the authenticated user {expected_uid}", cred.uid));
    }
    if ancestors.iter().take(MAX_ANCESTORS).any(|p| *p == cred.pid) {
        return Ok(());
    }
    Err(format!("peer process {} is not the one that started the helper", cred.pid))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SOCK: StreamId = StreamId { is_socket: true, dev: 8, ino: 1234 };
    const ME: PeerCred = PeerCred { pid: 500, uid: 1000 };

    #[test]
    fn the_gui_that_started_the_helper_is_accepted() {
        assert_eq!(verify_peer(SOCK, SOCK, ME, 1000, &[500, 1, 0]), Ok(()), "pkexec: the GUI is the parent");
        assert_eq!(verify_peer(SOCK, SOCK, ME, 1000, &[700, 600, 500, 1]), Ok(()), "sudo/doas: two processes in between");
        assert_eq!(verify_peer(SOCK, SOCK, ME, 1000, &[700, 650, 600, 500]), Ok(()), "the fourth generation still counts");
    }

    #[test]
    fn a_pipe_or_a_file_is_not_the_socket() {
        let pipe = StreamId { is_socket: false, ..SOCK };
        let e = verify_peer(pipe, pipe, ME, 1000, &[500]).unwrap_err();
        assert!(e.contains("not the socket PortageHub provides"), "{e}");
        // Two different sockets (stdout redirected elsewhere) are refused too.
        let other = StreamId { ino: 99, ..SOCK };
        assert!(verify_peer(SOCK, other, ME, 1000, &[500]).is_err());
        let other_dev = StreamId { dev: 9, ..SOCK };
        assert!(verify_peer(SOCK, other_dev, ME, 1000, &[500]).is_err());
    }

    #[test]
    fn another_user_is_refused() {
        let e = verify_peer(SOCK, SOCK, PeerCred { uid: 1001, ..ME }, 1000, &[500]).unwrap_err();
        assert_eq!(e, "peer uid 1001 is not the authenticated user 1000");
        // Root asking is not the authenticated user either, unless that is who authenticated.
        assert!(verify_peer(SOCK, SOCK, PeerCred { uid: 0, ..ME }, 1000, &[500]).is_err());
        assert!(verify_peer(SOCK, SOCK, PeerCred { uid: 0, ..ME }, 0, &[500]).is_ok());
    }

    #[test]
    fn a_process_that_is_not_an_ancestor_is_refused() {
        let e = verify_peer(SOCK, SOCK, ME, 1000, &[700, 600, 400, 1]).unwrap_err();
        assert_eq!(e, "peer process 500 is not the one that started the helper");
        assert!(verify_peer(SOCK, SOCK, ME, 1000, &[]).is_err());
        // Five generations up is too far: a process that merely sits somewhere above is not enough.
        assert!(verify_peer(SOCK, SOCK, ME, 1000, &[700, 650, 600, 550, 500]).is_err());
    }
}
