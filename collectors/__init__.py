"""Collectors run in their own threads, write to the Store, and report their own
health. They never raise out of their loop: a broken collector is a health row
saying so, not a dead process. Every collector honours `stop` (threading.Event)."""
