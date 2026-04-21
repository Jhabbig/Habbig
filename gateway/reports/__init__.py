"""Weekly PDF intelligence reports — Pro feature.

Pipeline:
  jobs/generate_weekly_reports.py  runs every Mon 07:00 UTC
  reports/weekly.py                builds data + renders HTML/PDF
  reports_routes.py                in-app viewer + download route
  email_system                     attaches the PDF to the Monday digest

Rendering uses WeasyPrint when available; missing WeasyPrint degrades to
HTML-only reports (the job logs + the viewer still shows the prose).
"""

from reports.weekly import build_report_for_user  # noqa: F401
