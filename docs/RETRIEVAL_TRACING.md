# Local retrieval stage timings

Each `retrieve` invocation now emits one structured `retrieval_completed`
event through Jobrunner's existing local logger. `trace_id` and `task_id`
join existing API/worker/model timing records. `retrieval_id` distinguishes
multiple searches in the same trace, including calls outside a request.

The record contains total elapsed milliseconds, completion status, and the
stage calls that actually occurred: keyword ranking, metadata totals, dense
posting search, chunk search, candidate hydration, semantic candidates and
reranking. Each stage reports elapsed milliseconds, completion status, and
the returned cardinality when its result is a list or dict. Tuple results
have no cardinality: their element count would not describe candidates.

Times are **inclusive**. Nested calls overlap; do not sum stage durations.
Total time also includes uninstrumented interpretation, IDF loading, query
embedding, filtering, fusion and presentation work. Stage names describe
functions, not model calls: a reranking stage may complete with reranking
disabled or its existing graceful fallback. `success` means the function
returned normally, not that the search found a job or a model ran.

No question text, query terms, employer names, posting IDs, URLs, descriptions,
résumé content, model vectors, arguments, result bodies or exception messages
are recorded here. There is no exporter, new table, model, configuration,
prompt or retrieval-mode change. Existing logger retention and access rules
apply. Instrumentation errors cannot mask the search result or its error.

Stages are scoped with ContextVar to one invocation, reset on success,
failure and cancellation. Calls to helpers outside a traced retrieval add no
event. This is observational telemetry: it does not change filters, pools,
matching, counts, ranking, citations or the API response schema.
