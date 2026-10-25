"""PORT: the repository stage (the Portage tree)."""

from __future__ import annotations

from ..news import news_summary, parse_news_list
from ..proxy import proxy_environment
from ..runner import RunnerError
from .portage import PortageNotes


class RepositoryStage(PortageNotes):
    def _repository(self) -> None:
        self.configurer.notify = self.log
        # make.conf first: emerge-webrsync reads GENTOO_MIRRORS from it, and a proxy has to be
        # known before the first download.
        hardware = getattr(self.inspection, "hardware", None) or None
        for note in self.configurer.configure_portage(self.manifest, self._plan(), hardware=hardware):
            self._portage_note(note)
        environment = proxy_environment(self.manifest.network.proxy)
        if self.configurer.sync_mode(self.manifest):
            try:
                self.configurer.chroot(("emaint", "sync", "--auto"), env=environment)
            except RunnerError as exc:
                if self.dry_run:
                    raise
                # rsync (port 873) is often blocked; the web snapshot is checked against Gentoo's signature too.
                self._portage_note("The package tree could not be synced with rsync (" + str(exc.output).strip()[-150:] + "); "
                                   "the web snapshot is used instead.")
                self.configurer.chroot(("emerge-webrsync",), env=environment)
        else:
            self.configurer.chroot(("emerge-webrsync",), env=environment)
        self._read_news()

    def _read_news(self) -> None:
        """Gentoo news after the tree is there (Handbook: Reading news items). It never stops the installation."""
        if self.dry_run:
            return
        try:
            result = self.configurer.chroot(("eselect", "news", "list"), capture=True)
            summary = news_summary(parse_news_list(result.stdout))
        except Exception as exc:  # noqa: BLE001 - news are advice; a failing eselect must not fail the stage
            self.log(f"Gentoo news could not be read ({exc})")
            return
        if summary:
            self.log(f"Gentoo news: {summary['unread']} unread item(s)")
            self.emit("progress", "repository", "Gentoo news", news=summary)
