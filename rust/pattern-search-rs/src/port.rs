//! The Rust port of `pattern_search.py`: whether a Python regular expression is found in a
//! line, over the syntax that module takes, and the same answer for a whole vocabulary of
//! patterns in one pass over the line (`SearchesIndex`).
//!
//! Exact rather than near. Rust's case folding and `\s` are not Python's: Rust's `(?i)i` misses
//! the dotted and dotless i that Python's matches, and its `\s` lacks `\x1c`-`\x1f`. So neither
//! is ever used. A pattern is translated with every literal, class and `\s` written out as the
//! characters Python matches, measured on Python 3.12.13 over every code point.
//!
//! The index runs every pattern with its lookarounds removed as one `RegexSet`, which can only
//! find more than the pattern does, then confirms the few it found with the whole pattern.

#![forbid(unsafe_code)]

use std::collections::BTreeSet;
use std::fmt::Write as _;

use fancy_regex::Regex as FancyRegex;
use regex::{Regex, RegexSet};

/// Characters that are not literals.
const SPECIAL: &str = ".^$*+?{}[]\\|()";
const QUANTIFIERS: &str = "?*+";
/// A bounded repeat takes at most this many, as `pattern_search._MAX_REPEAT`.
const MAX_REPEAT: u32 = 1000;
/// Not taken inside a class: escapes, nesting, negation, and what Rust reads as set operations.
const NOT_IN_CLASS: &str = "\\[^-&~|";
/// Group openers, longest first where one is a prefix of another.
const GROUPS: [&str; 8] = ["(?<=", "(?<!", "(?-i:", "(?i:", "(?:", "(?=", "(?!", "("];
const LOOKBEHINDS: [&str; 2] = ["(?<=", "(?<!"];
const LOOKAHEADS: [&str; 2] = ["(?=", "(?!"];
const IGNORE_CASE_PREFIX: &str = "(?i)";

/// Python's `\s` in a str pattern, `str.isspace`: Unicode's White_Space plus `\x1c`-`\x1f`.
const PYTHON_SPACE: &str = r"[\t\n\x0B\x0C\r\x1C-\x20\x{85}\x{A0}\x{1680}\x{2000}-\x{200A}\x{2028}\x{2029}\x{202F}\x{205F}\x{3000}]";

/// What Python's `re.IGNORECASE` also matches for an ASCII letter, beyond its two cases.
fn case_partners(letter: char) -> &'static [char] {
    match letter.to_ascii_lowercase() {
        'i' => &['\u{130}', '\u{131}'],
        'k' => &['\u{212A}'],
        's' => &['\u{17F}'],
        _ => &[],
    }
}

/// Every character a literal `c` matches, under `ignore_case` or not.
fn matched_by(c: char, ignore_case: bool, into: &mut BTreeSet<char>) {
    into.insert(c);
    if ignore_case && c.is_ascii_alphabetic() {
        into.insert(c.to_ascii_lowercase());
        into.insert(c.to_ascii_uppercase());
        into.extend(case_partners(c));
    }
}

fn class_of(chars: &BTreeSet<char>) -> String {
    let mut out = String::with_capacity(chars.len() * 8 + 2);
    out.push('[');
    for c in chars {
        let _ = write!(out, "\\x{{{:X}}}", u32::from(*c));
    }
    out.push(']');
    out
}

fn unsupported(at: usize) -> String {
    format!("ValueError: unsupported pattern syntax at position {at}")
}

/// A pattern in Rust syntax: `exact` as written, `loose` without its lookarounds.
struct Translation {
    exact: String,
    loose: String,
    has_lookaround: bool,
}

/// Walks a Python pattern once, as `pattern_search._Checker` does, refusing at the same
/// position, and writes its Rust translation as it goes.
struct Translator {
    chars: Vec<char>,
    at: usize,
    out: Translation,
    /// How many lookarounds enclose the current position: their text stays out of `loose`.
    hidden: usize,
}

impl Translator {
    fn translate(pattern: &str) -> Result<Translation, String> {
        let mut this = Self {
            chars: pattern.chars().collect(),
            at: 0,
            out: Translation {
                exact: String::new(),
                loose: String::new(),
                has_lookaround: false,
            },
            hidden: 0,
        };
        let ignore_case = pattern.starts_with(IGNORE_CASE_PREFIX);
        if ignore_case {
            this.at = IGNORE_CASE_PREFIX.len();
        }
        this.alternation(ignore_case, false)?;
        if this.at < this.chars.len() {
            return Err(unsupported(this.at));
        }
        Ok(this.out)
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.at).copied()
    }

    fn peek_in(&self, set: &str) -> bool {
        self.peek().is_some_and(|c| set.contains(c))
    }

    fn starts_with(&self, text: &str) -> bool {
        text.chars()
            .enumerate()
            .all(|(offset, c)| self.chars.get(self.at + offset) == Some(&c))
    }

    fn emit(&mut self, text: &str) {
        self.out.exact.push_str(text);
        if self.hidden == 0 {
            self.out.loose.push_str(text);
        }
    }

    /// Whether any branch has a character in it.
    fn alternation(&mut self, ignore_case: bool, lookbehind: bool) -> Result<bool, String> {
        let mut consumes = self.sequence(ignore_case, lookbehind)?;
        while self.peek() == Some('|') {
            if lookbehind {
                return Err(unsupported(self.at));
            }
            self.emit("|");
            self.at += 1;
            consumes |= self.sequence(ignore_case, lookbehind)?;
        }
        Ok(consumes)
    }

    fn sequence(&mut self, ignore_case: bool, lookbehind: bool) -> Result<bool, String> {
        let mut consumes = false;
        while let Some(c) = self.peek().filter(|c| *c != '|' && *c != ')') {
            let atom = self.atom(c, ignore_case, lookbehind)?;
            consumes |= atom;
            if self.at_quantifier() {
                // fancy-regex will not repeat a group that can only match nothing.
                if lookbehind || !atom {
                    return Err(unsupported(self.at));
                }
                self.quantifier()?;
                if self.at_quantifier() {
                    return Err(unsupported(self.at));
                }
            }
        }
        Ok(consumes)
    }

    fn at_quantifier(&self) -> bool {
        self.peek_in(QUANTIFIERS) || self.peek() == Some('{')
    }

    /// `?`, `*`, `+`, or `{m}`, `{m,n}`, `{m,}` with at most `MAX_REPEAT`, written as is.
    fn quantifier(&mut self) -> Result<(), String> {
        let start = self.at;
        if self.peek() != Some('{') {
            let quantifier: String = self.chars[start..=start].iter().collect();
            self.emit(&quantifier);
            self.at += 1;
            return Ok(());
        }
        let digits = |this: &mut Self| -> String {
            let begin = this.at;
            while this.peek().is_some_and(|c| c.is_ascii_digit()) {
                this.at += 1;
            }
            this.chars[begin..this.at].iter().collect()
        };
        self.at += 1;
        let low = digits(self);
        let comma = self.peek() == Some(',');
        if comma {
            self.at += 1;
        }
        let high = if comma { digits(self) } else { String::new() };
        if low.is_empty() || self.peek() != Some('}') {
            return Err(unsupported(start));
        }
        self.at += 1;
        let number = |text: &str| text.parse::<u32>().unwrap_or(u32::MAX);
        let top = if high.is_empty() {
            number(&low)
        } else {
            number(&high)
        };
        if number(&low) > MAX_REPEAT
            || top > MAX_REPEAT
            || (!high.is_empty() && number(&low) > number(&high))
        {
            return Err(unsupported(start));
        }
        let written: String = self.chars[start..self.at].iter().collect();
        self.emit(&written);
        Ok(())
    }

    /// The atom starting with `c`; whether it has a character in it, so a quantifier may follow.
    fn atom(&mut self, c: char, ignore_case: bool, lookbehind: bool) -> Result<bool, String> {
        let mut chars = BTreeSet::new();
        match c {
            '(' if lookbehind => return Err(unsupported(self.at)),
            '(' => return self.group(ignore_case),
            '[' => return self.class(ignore_case).map(|()| true),
            '\\' => match self.chars.get(self.at + 1).copied() {
                Some('s') => {
                    self.emit(PYTHON_SPACE);
                    self.at += 2;
                    return Ok(true);
                }
                Some('S') => {
                    self.emit(&PYTHON_SPACE.replacen('[', "[^", 1));
                    self.at += 2;
                    return Ok(true);
                }
                Some(e) if e.is_ascii_punctuation() || e == ' ' => {
                    matched_by(e, ignore_case, &mut chars);
                    self.at += 2;
                }
                _ => return Err(unsupported(self.at)),
            },
            '.' => {
                self.emit(".");
                self.at += 1;
                return Ok(true);
            }
            _ if SPECIAL.contains(c) || (ignore_case && !c.is_ascii()) => {
                return Err(unsupported(self.at));
            }
            _ => {
                matched_by(c, ignore_case, &mut chars);
                self.at += 1;
            }
        }
        self.emit(&class_of(&chars));
        Ok(true)
    }

    fn group(&mut self, ignore_case: bool) -> Result<bool, String> {
        let opener = GROUPS
            .into_iter()
            .find(|o| self.starts_with(o))
            .filter(|o| *o != "(" || !self.starts_with("(?"))
            .ok_or_else(|| unsupported(self.at))?;
        self.at += opener.chars().count();
        let ignore_case = match opener {
            "(?i:" => true,
            "(?-i:" => false,
            _ => ignore_case,
        };
        let lookbehind = LOOKBEHINDS.contains(&opener);
        let lookaround = lookbehind || LOOKAHEADS.contains(&opener);
        if lookbehind && matches!(self.peek(), None | Some(')')) {
            return Err(unsupported(self.at));
        }
        if lookaround {
            self.out.has_lookaround = true;
            self.hidden += 1;
            self.emit(opener);
        } else {
            self.emit("(?:");
        }
        let consumes = self.alternation(ignore_case, lookbehind)?;
        if self.peek() != Some(')') {
            return Err(unsupported(self.at));
        }
        self.emit(")");
        self.at += 1;
        if lookaround {
            self.hidden -= 1;
        }
        Ok(consumes && !lookaround)
    }

    fn class(&mut self, ignore_case: bool) -> Result<(), String> {
        self.at += 1; // the `[`
        if matches!(self.peek(), None | Some(']' | '^')) {
            return Err(unsupported(self.at));
        }
        let mut chars = BTreeSet::new();
        while self.peek() != Some(']') {
            let c = match self.peek() {
                Some(c) if !NOT_IN_CLASS.contains(c) && (' '..='~').contains(&c) => c,
                _ => return Err(unsupported(self.at)),
            };
            self.at += 1;
            let mut end = c;
            if self.peek() == Some('-') {
                self.at += 1;
                end = match self.peek() {
                    Some(e) if c.is_ascii_alphanumeric() && e.is_ascii_alphanumeric() => e,
                    _ => return Err(unsupported(self.at)),
                };
                if end < c {
                    return Err(unsupported(self.at));
                }
                self.at += 1;
            }
            for member in c..=end {
                matched_by(member, ignore_case, &mut chars);
            }
        }
        self.at += 1; // the `]`
        self.emit(&class_of(&chars));
        Ok(())
    }
}

/// The whole pattern, on the engine it needs: the `regex` crate unless it has a lookaround.
enum Matcher {
    Plain(Regex),
    Fancy(FancyRegex),
}

impl Matcher {
    fn new(translation: &Translation) -> Result<Self, String> {
        if translation.has_lookaround {
            FancyRegex::new(&translation.exact)
                .map(Self::Fancy)
                .map_err(|e| format!("RuntimeError: {e}"))
        } else {
            Regex::new(&translation.exact)
                .map(Self::Plain)
                .map_err(|e| format!("RuntimeError: {e}"))
        }
    }

    fn is_match(&self, line: &str) -> Result<bool, String> {
        match self {
            Self::Plain(regex) => Ok(regex.is_match(line)),
            Self::Fancy(regex) => regex
                .is_match(line)
                .map_err(|e| format!("RuntimeError: {e}")),
        }
    }
}

/// Port of `searches` (`pattern_search.py:40`).
pub fn searches(pattern: String, line: String) -> Result<bool, String> {
    let translation = Translator::translate(&pattern)?;
    Matcher::new(&translation)?.is_match(&line)
}

/// One pattern of an index: refused, found by the set alone, or confirmed after it.
enum Entry {
    Refused(String),
    Exact,
    Confirm(Matcher),
}

/// `searches` for many patterns against one line at a time: the patterns are translated and
/// compiled once, and each line is read by one `RegexSet` before any pattern is confirmed.
pub struct SearchesIndex {
    entries: Vec<Entry>,
    /// The set's pattern `k` is the loose form of `entries[slots[k]]`.
    slots: Vec<usize>,
    set: Result<RegexSet, String>,
    /// The first refused pattern's error, in item order.
    refused: Option<String>,
}

impl SearchesIndex {
    pub fn new(items: Vec<String>) -> Self {
        let mut entries = Vec::with_capacity(items.len());
        let mut slots = Vec::with_capacity(items.len());
        let mut loose = Vec::with_capacity(items.len());
        for (slot, pattern) in items.iter().enumerate() {
            let entry = Translator::translate(pattern).and_then(|translation| {
                let entry = if translation.has_lookaround {
                    Entry::Confirm(Matcher::new(&translation)?)
                } else {
                    Entry::Exact
                };
                slots.push(slot);
                loose.push(translation.loose);
                Ok(entry)
            });
            entries.push(entry.unwrap_or_else(Entry::Refused));
        }
        let refused = entries.iter().find_map(|entry| match entry {
            Entry::Refused(error) => Some(error.clone()),
            _ => None,
        });
        let set = RegexSet::new(&loose).map_err(|e| format!("RuntimeError: {e}"));
        Self {
            entries,
            slots,
            set,
            refused,
        }
    }

    /// The result of `searches(item, line)` for every item, in order; the first refused
    /// item's error when there is one.
    pub fn run(&self, line: String) -> Result<Vec<bool>, String> {
        if let Some(error) = &self.refused {
            return Err(error.clone());
        }
        let set = self.set.as_ref().map_err(Clone::clone)?;
        let mut found = vec![false; self.entries.len()];
        for k in set.matches(&line).iter() {
            let slot = self.slots[k];
            found[slot] = match &self.entries[slot] {
                Entry::Exact => true,
                Entry::Confirm(matcher) => matcher.is_match(&line)?,
                // INFALLIBLE: a refused pattern has no slot in the set, and `refused` returned.
                Entry::Refused(error) => return Err(error.clone()),
            };
        }
        Ok(found)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn search(pattern: &str, line: &str) -> Result<bool, String> {
        searches(pattern.to_string(), line.to_string())
    }

    #[test]
    fn case_partners_are_pythons_not_rusts() {
        assert_eq!(search("(?i)linux", "L\u{131}nux"), Ok(true));
        assert_eq!(search("(?i)linux", "L\u{130}NUX"), Ok(true));
        assert_eq!(search("(?i)k8s", "\u{212A}8\u{17F}"), Ok(true));
        assert_eq!(search("linux", "L\u{131}nux"), Ok(false));
        assert_eq!(search("(?i)ss", "\u{DF}"), Ok(false));
    }

    #[test]
    fn whitespace_is_pythons() {
        assert_eq!(search(r"a\sb", "a\u{1C}b"), Ok(true));
        assert_eq!(search(r"a\sb", "a\u{200B}b"), Ok(false));
        assert_eq!(search(r"a\sb", "a\u{3000}b"), Ok(true));
    }

    #[test]
    fn a_lookbehind_class_folds_too() {
        let go = r"(?i)(?<![A-Za-z0-9+#])(?:golang)";
        assert_eq!(search(go, "\u{212A}golang"), Ok(false));
        assert_eq!(search(go, "\u{E9}golang"), Ok(true));
        assert_eq!(search(r"(?<![A-Za-z0-9+#])Go", "\u{212A}Go"), Ok(true));
    }

    #[test]
    fn refusals_name_the_position() {
        assert_eq!(search("./x/**", "a"), Err(unsupported(5)));
        assert_eq!(search("(?i)\u{E9}", "a"), Err(unsupported(4)));
        assert_eq!(search("[a-]", "a"), Err(unsupported(3)));
        assert_eq!(search("(?<!a|b)c", "c"), Err(unsupported(5)));
        assert_eq!(search("(?=a)?", "a"), Err(unsupported(5)));
        assert_eq!(search("a)", "a"), Err(unsupported(1)));
        assert_eq!(search("(a", "a"), Err(unsupported(2)));
    }

    #[test]
    fn bounded_repeats_and_non_space() {
        assert_eq!(search("a{2}b", "aab"), Ok(true));
        assert_eq!(search("a{2}b", "ab"), Ok(false));
        assert_eq!(search("(?:x\\s+){0,2}y", "x x y"), Ok(true));
        assert_eq!(search("a{2,}", "aa"), Ok(true));
        assert_eq!(search("a{,3}", "a"), Err(unsupported(1)));
        assert_eq!(search("a{3,2}", "a"), Err(unsupported(1)));
        assert_eq!(search("a{1001}", "a"), Err(unsupported(1)));
        assert_eq!(search("a{2}{3}", "a"), Err(unsupported(4)));
        assert_eq!(search("a{x}", "a"), Err(unsupported(1)));
        assert_eq!(search("\\S", "\u{1C}"), Ok(false));
        assert_eq!(search("\\S", "\u{200B}"), Ok(true));
        assert_eq!(search("(?<!\\S)go", "a go"), Ok(true));
    }

    #[test]
    fn only_a_group_with_a_character_in_it_repeats() {
        assert_eq!(search("(?:)+(?!a)", "b"), Err(unsupported(4)));
        assert_eq!(search("(?:(?=a))*b", "b"), Err(unsupported(9)));
        assert_eq!(search("(?i)(?<=[Ks])()+", "k"), Err(unsupported(15)));
        assert_eq!(search("(?:a|)*b(?!c)", "b"), Ok(true));
        assert_eq!(search("(?:(?=a)b)*c", "ab"), Ok(false));
    }

    #[test]
    fn lookarounds_hold() {
        assert_eq!(search(r"Java(?!\s*Script)", "Java Script"), Ok(false));
        assert_eq!(search(r"Java(?!\s*Script)", "Java, Script"), Ok(true));
        assert_eq!(search(r"Go(?=\s*(?:,|/))", "Go, Rust"), Ok(true));
        assert_eq!(search(r"Go(?=\s*(?:,|/))", "Google"), Ok(false));
    }

    #[test]
    fn the_index_answers_as_the_function_does_item_by_item() {
        let patterns: Vec<String> = [
            r"(?i)(?<![A-Za-z0-9+#])(?:Python)(?![A-Za-z0-9+#]|\.[A-Za-z])",
            r"(?<![A-Za-z0-9+#])(?:Golang|Go(?=\s*(?:,|/|\)|and\s+[A-Z])))(?![A-Za-z0-9+#]|\.[A-Za-z])",
            r"(?i)(?<![A-Za-z0-9+#])(?:Kubernetes|K8s)(?![A-Za-z0-9+#]|\.[A-Za-z])",
            r"(?i)(?-i:REST)",
            "a.c",
            "",
        ]
        .map(String::from)
        .to_vec();
        let index = SearchesIndex::new(patterns.clone());
        for line in [
            "Python, Go, and K8s",
            "pythonic Google",
            "\u{212A}8\u{17F} and Go/Rust",
            "rest a\nc",
            "REST abc",
            "",
        ] {
            let expected: Result<Vec<bool>, String> = patterns
                .iter()
                .map(|p| searches(p.clone(), line.to_string()))
                .collect();
            assert_eq!(index.run(line.to_string()), expected, "{line:?}");
        }
    }

    #[test]
    fn the_index_reports_the_first_refused_pattern() {
        let index = SearchesIndex::new(vec!["a".into(), "(?i)\u{E9}".into(), "[".into()]);
        assert_eq!(index.run("a".into()), Err(unsupported(4)));
    }
}
