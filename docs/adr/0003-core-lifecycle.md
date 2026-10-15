# Keep lifecycle ownership in the execution core

Adapters translate native protocols. Admission, process ownership, capacity, receipts
and cleanup remain in the core so a provider extension cannot silently change their
meaning. Host supervisors broker child launches because native tool processes may
terminate their background descendants when a tool returns.
