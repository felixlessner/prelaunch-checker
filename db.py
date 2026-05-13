import os
from datetime import datetime
from typing import Optional, Dict, Any

from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    DateTime,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://datenbank_blai_user:6wkBpt0VzTMNwdtjBBG074uoxQ2SraWh@dpg-d823bqkvikkc73eaoim0-a.frankfurt-postgres.render.com/datenbank_blai",
)


print("Using DATABASE_URL:", DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    echo=False,      # bei Bedarf auf True für SQL-Debugging
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)

Base = declarative_base()


class WebsiteCheck(Base):
    """
    Speichert einen kompletten Check-Lauf.

    - id:        job_id oder generierte ID (hier: job_id-String)
    - start_url: vom Nutzer eingegebene URL
    - customer_id: optionaler Zuordnungs-Token (Newsletter/CRM)
    - result_json: kompletter Ergebnis-JSON (site_summary, pages, broken_links)
    """
    __tablename__ = "website_checks"

    id = Column(String, primary_key=True)  # z.B. job_id
    start_url = Column(Text, nullable=False)
    customer_id = Column(String, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

    crawled_count = Column(Integer, nullable=True)
    broken_count = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="done")

    # Nur in Postgres verfügbar; ideal für Auswertungen
    result_json = Column(JSONB, nullable=True)


def init_db() -> None:
    """Tabellen in der Datenbank anlegen (falls noch nicht vorhanden)."""
    Base.metadata.create_all(bind=engine)


def save_check_to_db(
    check_id: str,
    start_url: str,
    result: Dict[str, Any],
    customer_id: Optional[str] = None,
    status: str = "done",
) -> None:
    """
    Speichert einen einzelnen Check-Lauf in der Datenbank.

    - check_id:   z.B. job_id (UUID-String)
    - start_url:  ursprüngliche Eingabe-URL
    - result:     Dict mit site_summary, pages, broken_links, crawled_count
    - customer_id: optionaler Zuordnungs-Token
    - status:     z.B. "done" oder "failed"
    """
    crawled_count = result.get("crawled_count")
    broken_count = len(result.get("broken_links") or [])

    session = SessionLocal()
    try:
        wc = WebsiteCheck(
            id=check_id,
            start_url=start_url,
            customer_id=customer_id,
            created_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
            crawled_count=crawled_count,
            broken_count=broken_count,
            status=status,
            result_json=result,
        )
        session.add(wc)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
