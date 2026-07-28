"""腳本報告 dataclass（spec §4.2）。"""
from dataclasses import dataclass, field


@dataclass
class ConvertReport:
    files_scanned: int = 0
    files_changed: int = 0
    replacements: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        lines = [
            f"files_scanned={self.files_scanned} files_changed={self.files_changed} "
            f"replacements={self.replacements} dry_run={self.dry_run}",
        ]
        if self.errors:
            lines.append("errors:")
            lines.extend(f"  - {e}" for e in self.errors)
        return "\n".join(lines)


@dataclass
class MigrateReport:
    checkpoint_dir: str = ""
    output_dir: str = ""
    files_migrated: int = 0
    concepts_normalized: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"checkpoint_dir={self.checkpoint_dir}",
            f"output_dir={self.output_dir}",
            f"files_migrated={self.files_migrated} concepts_normalized={self.concepts_normalized}",
        ]
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)
