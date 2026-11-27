//! Python bindings for `pattern_search.py`: generated from the signatures in `port.rs` by rust-anything.
//! Regenerate with `rust-anything refresh` after changing them.

#![allow(clippy::too_many_arguments)]

use pyo3::prelude::*;

pub mod port;

/// Batches allocate on all cores at once; mimalloc keeps that from serializing.
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn error(message: String) -> PyErr {
    use pyo3::exceptions as e;
    /// `Err("Kind: text")` for a `Kind` the match below does not know: a built-in exception's name or
    /// `package.module.Class`, called with the text. None when it names no `Exception` subclass or the
    /// class cannot be made from the text alone (its constructor takes more arguments).
    fn named_error(kind: &str, text: &str) -> Option<PyErr> {
        Python::attach(|py| {
            // Each dotted part must be a Python identifier, by Python's own rules (`str.isidentifier`).
            let identifier = |part: &str| {
                pyo3::types::PyString::new(py, part)
                    .call_method0("isidentifier")
                    .and_then(|ok| ok.extract::<bool>())
                    .unwrap_or(false)
            };
            if !kind.split('.').all(identifier) {
                return None;
            }
            let (module, name) = kind.rsplit_once('.').unwrap_or(("builtins", kind));
            let class = py.import(module).ok()?.getattr(name).ok()?;
            let class = class.cast_into::<pyo3::types::PyType>().ok()?;
            if !class
                .is_subclass_of::<pyo3::exceptions::PyException>()
                .ok()?
            {
                return None;
            }
            let value = class.call1((text,)).ok()?;
            if !value.is_instance(&class).ok()? {
                return None;
            }
            Some(PyErr::from_value(value))
        })
    }
    let (kind, text) = message.split_once(": ").unwrap_or(("", message.as_str()));
    let text = text.to_string();
    match kind {
        "TypeError" => e::PyTypeError::new_err(text),
        "KeyError" => e::PyKeyError::new_err(text),
        "IndexError" => e::PyIndexError::new_err(text),
        "OverflowError" => e::PyOverflowError::new_err(text),
        "ZeroDivisionError" => e::PyZeroDivisionError::new_err(text),
        "AttributeError" => e::PyAttributeError::new_err(text),
        "RuntimeError" => e::PyRuntimeError::new_err(text),
        "NotImplementedError" => e::PyNotImplementedError::new_err(text),
        "LookupError" => e::PyLookupError::new_err(text),
        "ArithmeticError" => e::PyArithmeticError::new_err(text),
        "UnicodeError" => e::PyUnicodeError::new_err(text),
        "ValueError" => e::PyValueError::new_err(text),
        _ => named_error(kind, &text).unwrap_or_else(|| e::PyValueError::new_err(message)),
    }
}

fn searches_inner(pattern: String, line: String) -> Result<bool, String> {
    let out = port::searches(pattern, line)?;
    Ok(out)
}

/// `searches`.
#[pyfunction]
#[pyo3(name = "searches", signature = (pattern, line))]
fn searches(pattern: String, line: String) -> PyResult<bool> {
    searches_inner(pattern, line).map_err(error)
}

/// `searches` over many `pattern` values kept in Rust: `run` maps them all in one call, on all
/// cores, without the GIL; `rank` returns the indexes of results >= `min`, best first.
#[pyclass(name = "SearchesBatch")]
pub struct SearchesBatch {
    inner: port::SearchesIndex,
}

#[pymethods]
impl SearchesBatch {
    #[new]
    fn new(items: Vec<String>) -> Self {
        Self {
            inner: port::SearchesIndex::new(items),
        }
    }

    /// The result for every item, in order.
    fn run(&self, py: Python<'_>, line: String) -> PyResult<Vec<bool>> {
        py.detach(|| self.inner.run(line)).map_err(error)
    }
}

#[pymodule]
#[pyo3(name = "_native")]
fn native_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(searches, m)?)?;
    m.add_class::<SearchesBatch>()?;
    Ok(())
}
