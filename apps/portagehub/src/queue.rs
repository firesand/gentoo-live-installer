//! The queue between the request handlers and the thread that writes events to the GUI.
//!
//! An emerge can print faster than a stalled GUI reads. An unbounded channel would let the helper
//! grow without limit, so output (`Line` and `Partial`) is bounded: when the queue is full the
//! oldest output is dropped and one `Notice` says how much once things recover. Everything that
//! carries a result (`Started`, `Exit`, `Ok`, `Err`, `Data`, `Ready`, `Notice`) is never dropped.

use crate::protocol::Event;
use std::collections::VecDeque;
use std::sync::{Condvar, Mutex};

struct Inner {
    events: VecDeque<Event>,
    /// Output events dropped since the last notice.
    dropped: usize,
    closed: bool,
}

pub struct EventQueue {
    inner: Mutex<Inner>,
    cond: Condvar,
    cap: usize,
}

fn droppable(ev: &Event) -> bool {
    matches!(ev, Event::Line { .. } | Event::Partial { .. })
}

impl EventQueue {
    pub fn new(cap: usize) -> EventQueue {
        EventQueue { inner: Mutex::new(Inner { events: VecDeque::new(), dropped: 0, closed: false }), cond: Condvar::new(), cap }
    }

    pub fn push(&self, ev: Event) {
        let mut g = self.inner.lock().unwrap();
        if g.events.len() >= self.cap && droppable(&ev) {
            // Keep the newest output: the end of a failed build matters more than its start.
            match g.events.iter().position(droppable) {
                Some(i) => {
                    g.events.remove(i);
                    g.dropped += 1;
                }
                None => {
                    g.dropped += 1;
                    return;
                }
            }
        }
        g.events.push_back(ev);
        self.cond.notify_one();
    }

    /// The next event, waiting for one; `None` once the queue is closed and empty.
    pub fn pop(&self) -> Option<Event> {
        let mut g = self.inner.lock().unwrap();
        loop {
            // Recovered: tell the GUI what it missed before the output continues.
            if g.dropped > 0 && g.events.len() <= self.cap / 2 {
                let n = std::mem::take(&mut g.dropped);
                return Some(Event::Notice { msg: format!("{n} output line{} dropped because the display could not keep up", if n == 1 { " was" } else { "s were" }) });
            }
            if let Some(ev) = g.events.pop_front() {
                return Some(ev);
            }
            if g.closed {
                // Nothing left, but a drop that never recovered is still worth a line.
                if g.dropped > 0 {
                    let n = std::mem::take(&mut g.dropped);
                    return Some(Event::Notice { msg: format!("{n} output lines were dropped because the display could not keep up") });
                }
                return None;
            }
            g = self.cond.wait(g).unwrap();
        }
    }

    pub fn close(&self) {
        self.inner.lock().unwrap().closed = true;
        self.cond.notify_all();
    }

    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().events.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::StreamKind;

    fn line(n: u64) -> Event {
        Event::Line { id: 1, stream: StreamKind::Stdout, text: format!("line {n}") }
    }

    fn drain(q: &EventQueue) -> Vec<Event> {
        q.close();
        std::iter::from_fn(|| q.pop()).collect()
    }

    #[test]
    fn output_beyond_the_cap_drops_the_oldest_and_says_so_once() {
        let q = EventQueue::new(10);
        q.push(Event::Started { id: 1, pid: 5 });
        for n in 0..30 {
            q.push(line(n));
        }
        q.push(Event::Exit { id: 1, code: Some(0), signal: None });
        assert_eq!(q.len(), 11, "the cap bounds the output (Started + 9 lines); the exit always fits");
        let got = drain(&q);
        assert!(matches!(got[0], Event::Started { id: 1, .. }), "the start is never dropped");
        assert!(matches!(got.last(), Some(Event::Exit { id: 1, .. })), "neither is the exit");
        let texts: Vec<&str> = got.iter().filter_map(|e| if let Event::Line { text, .. } = e { Some(text.as_str()) } else { None }).collect();
        assert_eq!(texts.len(), 9, "{texts:?}");
        assert_eq!(texts.last(), Some(&"line 29"), "the newest output survives");
        let notices: Vec<&Event> = got.iter().filter(|e| matches!(e, Event::Notice { .. })).collect();
        assert_eq!(notices.len(), 1, "one notice, not one per dropped line");
        match notices[0] {
            Event::Notice { msg } => assert!(msg.starts_with("21 output lines were dropped"), "{msg}"),
            _ => unreachable!(),
        }
    }

    #[test]
    fn results_are_never_dropped_even_when_the_queue_is_full_of_them() {
        let q = EventQueue::new(3);
        for id in 0..10 {
            q.push(Event::Ok { id });
        }
        q.push(line(0));
        let got = drain(&q);
        assert_eq!(got.iter().filter(|e| matches!(e, Event::Ok { .. })).count(), 10);
        // The output event had nothing droppable to replace, so it was the one dropped.
        assert!(!got.iter().any(|e| matches!(e, Event::Line { .. })));
        assert!(got.iter().any(|e| matches!(e, Event::Notice { .. })));
    }

    #[test]
    fn nothing_is_lost_below_the_cap() {
        let q = EventQueue::new(100);
        for n in 0..50 {
            q.push(line(n));
        }
        let got = drain(&q);
        assert_eq!(got.len(), 50);
        assert!(!got.iter().any(|e| matches!(e, Event::Notice { .. })));
    }

    #[test]
    fn pop_waits_for_a_push_and_ends_on_close() {
        let q = std::sync::Arc::new(EventQueue::new(10));
        let q2 = q.clone();
        let t = std::thread::spawn(move || (q2.pop(), q2.pop()));
        std::thread::sleep(std::time::Duration::from_millis(50));
        q.push(Event::Ok { id: 9 });
        q.close();
        let (a, b) = t.join().unwrap();
        assert!(matches!(a, Some(Event::Ok { id: 9 })) && b.is_none());
    }
}
