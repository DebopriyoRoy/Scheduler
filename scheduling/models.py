from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Employee(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    display_name = models.CharField(max_length=201, unique=True)
    active = models.BooleanField(default=True)
    square_team_member_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    spirit_only_employment = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]

    def __str__(self) -> str:
        return self.display_name


class Role(models.Model):
    name = models.CharField(max_length=100, unique=True)
    square_job_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class EmployeeRole(models.Model):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="employee_roles",
    )
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="employee_roles")
    capability_level = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["employee__display_name", "role__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "role"],
                name="unique_employee_role",
            )
        ]

    def __str__(self) -> str:
        return f"{self.employee} - {self.role} (Level {self.capability_level})"


class SquareLocation(models.Model):
    name = models.CharField(max_length=255)
    square_location_id = models.CharField(max_length=100, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

