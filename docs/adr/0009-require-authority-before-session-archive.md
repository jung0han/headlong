# Require user authority before archiving a session

Model analysis may produce an Archive Candidate with evidence that work appears
complete, but it may not archive a Codex Session on that judgment alone. A user
must accept the candidate or issue an Archive Directive before an adapter calls
Codex's archive interface; the system never mutates session files directly.
This keeps completion inference advisory while allowing reversible archival.
