from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from scheduling.exports.common import POSITION_CODES, assumptions, show_export_rows
from scheduling.models import Employee
from scheduling.services.metrics import metrics_for_employee

NAVY = colors.HexColor("#173B63")
PALE_BLUE = colors.HexColor("#EAF2F8")


def _footer(canvas, document):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.grey)
    canvas.drawString(0.45 * inch, 0.3 * inch, "Spirit of Newfoundland — Local management schedule")
    canvas.drawRightString(10.55 * inch, 0.3 * inch, f"Page {document.page}")
    canvas.restoreState()


def _styled_table(data, widths, repeat_rows=1, font_size=7):
    table = Table(data, colWidths=widths, repeatRows=repeat_rows, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), font_size),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B7C3D0")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE_BLUE]),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def schedule_pdf_bytes(schedule_run) -> bytes:
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(letter),
        leftMargin=0.4 * inch,
        rightMargin=0.4 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.5 * inch,
        title=f"Spirit Schedule {schedule_run.start_date} to {schedule_run.end_date}",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "SpiritTitle",
        parent=styles["Title"],
        textColor=NAVY,
        alignment=TA_CENTER,
        fontSize=20,
        spaceAfter=8,
    )
    story = [
        Paragraph("Spirit of Newfoundland Management Schedule", title),
        Paragraph(
            f"{schedule_run.start_date:%B %d, %Y} – {schedule_run.end_date:%B %d, %Y} "
            f"| Status: {schedule_run.get_status_display()}",
            styles["Heading2"],
        ),
        Paragraph(
            "Local recommendation and approval record only. No shifts are published to Square.",
            styles["Italic"],
        ),
        Spacer(1, 10),
    ]
    schedule_data = [
        [
            "Date / Show",
            "Guests",
            "Lead",
            "Server 2",
            "Server 3",
            "OC Server",
            "Bartender",
            "OC Bar",
            "Busser",
            "50/50",
            "Warnings",
        ]
    ]
    for show, assignments, warnings in show_export_rows(schedule_run):
        people = [
            assignments.get(code).employee.display_name if assignments.get(code) else "SHORTAGE"
            for code in POSITION_CODES
        ]
        if not show.requires_50_50:
            people[-1] = "N/A"
        schedule_data.append(
            [
                f"{show.date:%a %b %d}\n{show.title}",
                str(show.planning_guest_count),
                *people,
                "\n".join(warning.get_warning_type_display() for warning in warnings) or "None",
            ]
        )
    story.append(
        _styled_table(
            schedule_data,
            [1.25 * inch, 0.45 * inch, *([0.8 * inch] * 8), 1.4 * inch],
            font_size=6.5,
        )
    )
    story.extend([PageBreak(), Paragraph("Employee Workload Summary", styles["Heading1"])])
    totals_data = [
        [
            "Employee",
            "Confirmed Hours",
            "Confirmed Shifts",
            "On Call",
            "Server",
            "Bar",
            "Busser",
            "50/50",
            "Weekend",
        ]
    ]
    for employee in Employee.objects.filter(active=True):
        metric = metrics_for_employee(employee, schedule_run)
        totals_data.append(
            [
                employee.display_name,
                str(metric.confirmed_paid_hours),
                metric.confirmed_shift_count,
                metric.on_call_assignment_count,
                metric.server_shift_count,
                metric.bartender_shift_count,
                metric.busser_shift_count,
                metric.fifty_fifty_count,
                metric.weekend_assignment_count,
            ]
        )
    story.append(_styled_table(totals_data, [1.75 * inch, *([1.05 * inch] * 8)], font_size=8))
    story.extend([Spacer(1, 14), Paragraph("Warnings", styles["Heading1"])])
    warning_data = [["Date", "Severity", "Type", "Message", "Resolved"]]
    for warning in schedule_run.warnings.select_related("show"):
        warning_data.append(
            [
                warning.show.date.isoformat() if warning.show else "Period",
                warning.get_severity_display(),
                warning.get_warning_type_display(),
                Paragraph(warning.message, styles["BodyText"]),
                "Yes" if warning.resolved else "No",
            ]
        )
    if len(warning_data) == 1:
        warning_data.append(["—", "—", "None", "No warnings were generated.", "—"])
    story.append(
        _styled_table(
            warning_data,
            [0.8 * inch, 0.9 * inch, 1.8 * inch, 6.0 * inch, 0.7 * inch],
            font_size=7,
        )
    )
    story.extend([Spacer(1, 14), Paragraph("Assumptions", styles["Heading1"])])
    assumption_data = [["Topic", "Detail"], *assumptions(schedule_run)]
    story.append(_styled_table(assumption_data, [1.5 * inch, 9.3 * inch], font_size=8))
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return output.getvalue()
