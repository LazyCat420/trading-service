"""
kannapedia_collector.py — Domain-agnostic Kannapedia strain data collection
---------------------------------------------------------------------------
Ported from kannapedia-scraper's kaana_scraper.py.
All visualization, CSV-writing, and analysis logic has been REMOVED.
This collector only knows HOW to extract strain data from Kannapedia —
the caller decides what to do with it.

Uses Playwright to render the JS-heavy Kannapedia pages and extract
structured data (metadata, chemicals, genetic relationships, blockchain).
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.scraper.engines.playwright_engine import PlaywrightEngine

logger = logging.getLogger(__name__)


@dataclass
class KannapediaStrain:
    """Normalized strain data from a single Kannapedia page."""

    rsp_number: str
    name: str = ""
    general_info: dict[str, str] = field(default_factory=dict)
    chemical_content: dict[str, dict[str, str]] = field(default_factory=dict)
    genetic_relationships: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    blockchain: dict[str, str] = field(default_factory=dict)
    source_url: str = ""
    scraped_at: datetime = field(default_factory=datetime.utcnow)


# The JS evaluation script — extracted verbatim from kaana_scraper.py
# to preserve the exact extraction logic that works against Kannapedia's DOM.
_EXTRACTION_JS = """
() => {
    const data = {
        name: '',
        general_info: {},
        chemical_content: { cannabinoids: {}, terpenoids: {} },
        genetic_relationships: { all_samples: [], base_tree: [], most_distant: [] },
        blockchain: {}
    };

    // Get strain name
    const titleElem = document.querySelector('h1.StrainInfo--title');
    data.name = titleElem ? titleElem.textContent.trim() : '';

    // Get general information
    const generalSection = Array.from(document.querySelectorAll('h2')).find(
        h2 => h2.textContent.includes('General Information')
    )?.parentElement;

    if (generalSection) {
        const rows = generalSection.querySelectorAll('dt, dd');
        for (let i = 0; i < rows.length; i += 2) {
            if (rows[i] && rows[i + 1]) {
                data.general_info[rows[i].textContent.trim()] = rows[i + 1].textContent.trim();
            }
        }
    }

    // Get grower information
    const growerText = document.querySelector('.StrainInfo--grower');
    if (growerText) {
        data.general_info['Grower'] = growerText.textContent.replace('Grower:', '').trim();
    }

    // Get chemical content
    const chemicalSection = Array.from(document.querySelectorAll('h2')).find(
        h2 => h2.textContent.includes('Chemical Information')
    )?.parentElement;

    if (chemicalSection) {
        const cannabinoidSection = Array.from(chemicalSection.querySelectorAll('h3')).find(
            h3 => h3.textContent.includes('Cannabinoids')
        );
        if (cannabinoidSection) {
            const items = Array.from(cannabinoidSection.parentElement.querySelectorAll('dt, dd'));
            for (let i = 0; i < items.length; i += 2) {
                if (items[i] && items[i + 1]) {
                    const value = items[i + 1].textContent.trim();
                    if (value !== 'n/a' && !value.toLowerCase().includes('no information')) {
                        data.chemical_content.cannabinoids[items[i].textContent.trim()] = value;
                    }
                }
            }
        }

        const terpenoidSection = Array.from(chemicalSection.querySelectorAll('h3')).find(
            h3 => h3.textContent.includes('Terpenoids')
        );
        if (terpenoidSection) {
            const items = Array.from(terpenoidSection.parentElement.querySelectorAll('dt, dd'));
            for (let i = 0; i < items.length; i += 2) {
                if (items[i] && items[i + 1]) {
                    const value = items[i + 1].textContent.trim();
                    if (value !== 'n/a' && !value.toLowerCase().includes('no information')) {
                        data.chemical_content.terpenoids[items[i].textContent.trim()] = value;
                    }
                }
            }
        }
    }

    // Get heterozygosity
    const heteroText = document.evaluate(
        "//text()[contains(., 'Heterozygosity:')]",
        document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
    ).singleNodeValue;
    if (heteroText) {
        const match = heteroText.textContent.match(/Heterozygosity:\\s*([\\d.]+%)/);
        if (match) data.general_info['Reported Heterozygosity'] = match[1];
    }

    // Get rarity
    const rarityText = document.evaluate(
        "//text()[contains(., 'Rarity:')]",
        document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
    ).singleNodeValue;
    if (rarityText) {
        const match = rarityText.textContent.match(/Rarity:\\s*(\\w+)/);
        if (match) data.general_info['Rarity'] = match[1];
    }

    // Extract genetic relationships
    const listItems = Array.from(document.querySelectorAll('li')).filter(li => {
        return li.textContent.trim().match(/^\\d+\\.\\d+\\s+.+\\(RSP\\d+\\)/);
    });

    const geneticSections = document.querySelectorAll('h3');
    geneticSections.forEach(section => {
        const title = section.textContent.trim().toLowerCase();
        const list = section.nextElementSibling;
        if (!list) return;

        const items = Array.from(list.querySelectorAll('li'));
        const results = items.map(li => {
            const text = li.textContent.trim();
            const match = text.match(/^(\\d+\\.\\d+)\\s+(.+?)\\s*\\((RSP\\d+)\\)/i);
            if (match) {
                return {
                    distance: parseFloat(match[1]),
                    strain: match[2].trim(),
                    rsp: match[3].toLowerCase()
                };
            }
            return null;
        }).filter(Boolean);

        if (title.includes('all samples')) {
            data.genetic_relationships.all_samples = results;
        } else if (title.includes('base tree')) {
            data.genetic_relationships.base_tree = results;
        } else if (title.includes('most genetically distant')) {
            data.genetic_relationships.most_distant = results;
        }
    });

    // Get blockchain information
    const txidElem = document.evaluate(
        "//dt[contains(text(), 'Transaction ID')]/following-sibling::dd[1]",
        document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
    ).singleNodeValue;
    if (txidElem) data.blockchain.txid = txidElem.textContent.trim();

    const shasumElem = document.evaluate(
        "//dt[contains(text(), 'SHASUM Hash')]/following-sibling::dd[1]",
        document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
    ).singleNodeValue;
    if (shasumElem) data.blockchain.shasum = shasumElem.textContent.trim();

    return data;
}
"""


def _normalize_rsp(rsp_input: str) -> str:
    """Normalize RSP number input to lowercase format like 'rsp10143'."""
    rsp = rsp_input.lower().strip()
    if not rsp.startswith("rsp"):
        rsp = "rsp" + rsp.replace("rsp", "")
    return rsp


class KannapediaCollector:
    """Collects strain data from Kannapedia.

    Uses Playwright to render the JS-heavy strain pages and extract
    structured data. This collector is purely about data extraction —
    no CSV writing, no charts, no analysis.

    Usage:
        collector = KannapediaCollector()
        strain = await collector.get_strain("rsp10143")
    """

    BASE_URL = "https://www.kannapedia.net/strains/"

    async def get_strain(self, rsp_number: str) -> KannapediaStrain:
        """Scrape a single strain page by RSP number.

        Args:
            rsp_number: RSP identifier (e.g. "rsp10143" or "10143").

        Returns:
            KannapediaStrain with all extracted data.

        Raises:
            Exception: If the page fails to load or data extraction fails.
        """
        rsp = _normalize_rsp(rsp_number)
        url = f"{self.BASE_URL}{rsp}"

        logger.info("[kannapedia] Scraping %s at %s", rsp, url)

        engine = PlaywrightEngine()
        result = await engine.fetch(url, {
            "wait_for": "h1.StrainInfo--title",
            "timeout": 30000,
            "evaluate": _EXTRACTION_JS,
        })

        if not result.success:
            raise Exception(f"Failed to scrape {url}: {result.error}")

        raw_data = result.data
        strain = KannapediaStrain(
            rsp_number=rsp.upper(),
            name=raw_data.get("name", ""),
            general_info=raw_data.get("general_info", {}),
            chemical_content=raw_data.get("chemical_content", {}),
            genetic_relationships=raw_data.get("genetic_relationships", {}),
            blockchain=raw_data.get("blockchain", {}),
            source_url=url,
        )

        logger.info(
            "[kannapedia] Scraped %s (%s): %d relatives, %d chemicals",
            strain.name,
            strain.rsp_number,
            sum(len(v) for v in strain.genetic_relationships.values()),
            sum(len(v) for v in strain.chemical_content.values()),
        )

        return strain

    async def get_strains(
        self,
        rsp_numbers: list[str],
        continue_on_error: bool = True,
    ) -> list[KannapediaStrain]:
        """Scrape multiple strain pages.

        Args:
            rsp_numbers: List of RSP identifiers.
            continue_on_error: If True, log errors and continue. If False, raise.

        Returns:
            List of successfully scraped KannapediaStrain objects.
        """
        results: list[KannapediaStrain] = []

        for rsp in rsp_numbers:
            try:
                strain = await self.get_strain(rsp)
                results.append(strain)
            except Exception as e:
                logger.error("[kannapedia] Error scraping %s: %s", rsp, e)
                if not continue_on_error:
                    raise

        logger.info(
            "[kannapedia] Scraped %d/%d strains successfully",
            len(results), len(rsp_numbers),
        )
        return results


def _serialize_strain(strain: KannapediaStrain) -> dict[str, Any]:
    """Convert KannapediaStrain to JSON-safe dict for API responses."""
    return {
        "rsp_number": strain.rsp_number,
        "name": strain.name,
        "general_info": strain.general_info,
        "chemical_content": strain.chemical_content,
        "genetic_relationships": strain.genetic_relationships,
        "blockchain": strain.blockchain,
        "source_url": strain.source_url,
        "scraped_at": strain.scraped_at.isoformat(),
    }
