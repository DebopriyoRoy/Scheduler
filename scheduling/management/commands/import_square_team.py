"""Bring Square's team into the roster, with the jobs Square assigns them.

The roster was seeded by hand with seventeen service staff under the short names
people are called by. Square carries thirty-nine active team members under their full
names, so the two lists could not be compared, and anyone hired in Square simply never
appeared here.

Roles come from each member's Square job assignments, never from a guess. Square's
team includes the kitchen, cleaners, a tech, managers and the owner; granting those
people a serving role because they exist would put them in the scheduling pool, which
is a silent and damaging change. Someone whose Square job has no counterpart here is
imported with no role, which is exactly what they are: a colleague this application
knows about and cannot roster.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from integrations.square.exceptions import SquareIntegrationError
from scheduling.models import (
    Employee,
    EmployeeRole,
    MappingStatus,
    Role,
    SquareEmployeeMapping,
    SquareRoleMapping,
)

# Square jobs that mean "not service staff". These people are imported so the roster
# matches Square, but never enter the scheduling pool.
NON_SCHEDULING_JOBS = {"manager", "owner"}

# The level a newly imported person starts at. Three is the base grade every existing
# member of staff holds unless someone raised it; guessing higher would hand a new
# starter the seniority the allocator uses to prefer them.
DEFAULT_CAPABILITY_LEVEL = 3


class Command(BaseCommand):
    help = "Import Square's active team as staff, with the roles Square gives them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        try:
            config = SquareConfig.from_env()
            if config.environment is not SquareEnvironment.PRODUCTION:
                raise CommandError("This reads the production team; set SQUARE_ENVIRONMENT.")
            client = SquareClient(config)
            members = client.search_team_members(active_only=True)
        except SquareIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        role_by_job_id, role_by_job_title = self._role_lookups()
        created = updated = renamed = roles_added = excluded = 0
        unmapped_jobs: set[str] = set()

        for member in sorted(members, key=lambda m: m.get("given_name", "")):
            square_id = member.get("id", "")
            given = (member.get("given_name") or "").strip()
            family = (member.get("family_name") or "").strip()
            full_name = f"{given} {family}".strip()
            if not square_id or not full_name:
                continue

            titles, job_ids = self._jobs_for(client, square_id)
            roles = self._roles_for(titles, job_ids, role_by_job_id, role_by_job_title)
            for title in titles:
                if title.strip().lower() not in NON_SCHEDULING_JOBS and not roles:
                    unmapped_jobs.add(title)

            employee = self._find_employee(square_id, full_name, given)
            is_new = employee is None
            # Someone whose only Square jobs are managerial never belongs in the pool,
            # whatever else changes about them.
            keep_out = bool(titles) and all(
                t.strip().lower() in NON_SCHEDULING_JOBS for t in titles
            )

            if dry_run:
                action = "CREATE" if is_new else (
                    "RENAME" if employee.display_name != full_name else "keep"
                )
                self.stdout.write(
                    f"  {action:<7} {full_name:<24} jobs={', '.join(titles) or '(none)':<28}"
                    f" roles={', '.join(sorted(r.name for r in roles)) or '(none)'}"
                    f"{'  [not schedulable]' if keep_out else ''}"
                )
                continue

            with transaction.atomic():
                if is_new:
                    employee = Employee.objects.create(
                        first_name=given or full_name,
                        last_name=family,
                        display_name=full_name,
                        active=True,
                        excluded_from_automatic_scheduling=keep_out,
                    )
                    created += 1
                else:
                    if employee.display_name != full_name:
                        employee.display_name = full_name
                        renamed += 1
                    employee.first_name = given or employee.first_name
                    employee.last_name = family or employee.last_name
                    if keep_out and not employee.excluded_from_automatic_scheduling:
                        employee.excluded_from_automatic_scheduling = True
                    employee.save()
                    updated += 1
                if employee.excluded_from_automatic_scheduling:
                    excluded += 1

                SquareEmployeeMapping.objects.update_or_create(
                    employee=employee,
                    environment=SquareEnvironment.PRODUCTION.value,
                    defaults={
                        "square_team_member_id": square_id,
                        "square_given_name": given,
                        "square_family_name": family,
                        "status": MappingStatus.MAPPED_EXACT,
                        "match_type": "SQUARE_TEAM_IMPORT",
                        "confidence_reason": "Imported directly from the Square team list.",
                    },
                )
                for role in roles:
                    _, made = EmployeeRole.objects.get_or_create(
                        employee=employee,
                        role=role,
                        defaults={
                            "capability_level": DEFAULT_CAPABILITY_LEVEL,
                            "active": True,
                        },
                    )
                    roles_added += bool(made)

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDry run - nothing written."))
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSquare team import: {created} created, {updated} updated, "
                f"{renamed} renamed to their Square name, {roles_added} role(s) granted, "
                f"{excluded} held out of automatic scheduling."
            )
        )
        if unmapped_jobs:
            self.stdout.write(
                "Square jobs with no role here (imported without one): "
                + ", ".join(sorted(unmapped_jobs))
            )

    def _role_lookups(self):
        by_id: dict[str, Role] = {}
        by_title: dict[str, Role] = {}
        for mapping in SquareRoleMapping.objects.select_related("role"):
            if not mapping.role_id:
                continue
            if mapping.square_job_id:
                by_id[mapping.square_job_id] = mapping.role
            title = (getattr(mapping, "square_job_title", "") or "").strip().lower()
            if title:
                by_title[title] = mapping.role
        return by_id, by_title

    def _jobs_for(self, client, square_id: str) -> tuple[list[str], list[str]]:
        """A team member's Square job titles and ids.

        Job assignments live on the wage setting, not on the team member, so this is a
        second request per person. A failure here must not invent roles: it returns
        nothing, and the person is imported without one.
        """
        try:
            payload = client._request("GET", f"/v2/team-members/{square_id}/wage-setting")
        except SquareIntegrationError:
            return [], []
        assignments = (payload.get("wage_setting") or {}).get("job_assignments") or []
        titles = [a.get("job_title", "").strip() for a in assignments if a.get("job_title")]
        ids = [a.get("job_id", "").strip() for a in assignments if a.get("job_id")]
        return titles, ids

    def _roles_for(self, titles, job_ids, by_id, by_title) -> list[Role]:
        roles: list[Role] = []
        for job_id in job_ids:
            role = by_id.get(job_id)
            if role and role not in roles:
                roles.append(role)
        for title in titles:
            role = by_title.get(title.strip().lower())
            if role and role not in roles:
                roles.append(role)
        return roles

    def _find_employee(self, square_id: str, full_name: str, given: str) -> Employee | None:
        mapping = SquareEmployeeMapping.objects.filter(
            square_team_member_id=square_id,
            environment=SquareEnvironment.PRODUCTION.value,
        ).select_related("employee").first()
        if mapping:
            return mapping.employee
        exact = Employee.objects.filter(display_name__iexact=full_name).first()
        if exact:
            return exact
        # The roster holds short names - "Khrystyna" for "Khrystyna Zavadetska" - so a
        # first-name match is how existing staff are recognised rather than duplicated.
        # Only when it is unambiguous: two people share a first name otherwise.
        candidates = Employee.objects.filter(display_name__iexact=given)
        return candidates.first() if candidates.count() == 1 else None
