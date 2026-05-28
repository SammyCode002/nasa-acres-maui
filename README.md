# NASA ACRES Maui: Live Fuel Moisture Content Mapping

Mapping wildfire risk in Maui County, Hawaiʻi using live fuel moisture content (LFMC) from satellite remote sensing and Earth-observation foundation models.

Status: literature review and project planning. Kickoff meeting with our advisor is pending.

## Project goal (from the internship description)

Develop capabilities for creating monthly LFMC maps over Maui County (for example, 2023 to 2026) as a wildfire-risk indicator. The approach follows Johnson et al. (2025): fine-tune an Earth-observation foundation model to predict LFMC from satellite inputs, trained on the Globe-LFMC dataset.

Deliverables: a GitHub repository with end-to-end mapping code, and monthly LFMC maps over Maui County for multiple years.

## Team

| Person | Role |
|---|---|
| Sam Dameg | Lead Fellow |
| Noah Munz | Intern |
| Ana Tárano, PhD | Advisor (ASU SCAI) |
| Hannah Kerner, PhD | Faculty Lead (ASU SCAI) |

Meetings: Mondays 8:00 AM HST, weekly to start, then bi-weekly.

## Questions to work through (from Ana)

These guide our reading and our first working meeting:

1. What would be a good approach to extend the LFMC methodology to Hawaiʻi?
2. Should we use OlmoEarth, Galileo, or both? Which training strategy should we first attempt (full fine-tuning, transfer learning, embeddings)?
3. What data do we need to collect for Hawaiʻi?
4. How should we divide tasks between Sam and Noah?

## Reading list (assigned by Ana)

| Type | Item |
|---|---|
| Read | Johnson et al. (2025), LFMC mapping: https://arxiv.org/pdf/2506.20132v2 |
| Read | Globe-LFMC 2.0 dataset: https://www.nature.com/articles/s41597-024-03159-6 |
| Read | OlmoEarth: https://arxiv.org/pdf/2511.13655 |
| Watch | OlmoEarth webinar (AI2): https://drive.google.com/file/d/1JZ4r3hoS99rPXw7dKngcr9mi5YZPsIql/view?usp=sharing |
| Review | Project scope doc (Google Doc from Ana) |
| Explore | FEMS, Fire Environment Mapping System: https://fems.fs2c.usda.gov |

## Next steps

- [ ] Read the papers and watch the OlmoEarth video
- [ ] Explore the FEMS fire-weather tool
- [ ] Sam and Noah meet Saturday to review the materials together
- [ ] Track hours so the project can be chunked appropriately
- [ ] Meet with Ana to debrief the site visit and papers, then plan the project
- [ ] Begin recreating CONUS results from OlmoEarth and Galileo

## Acknowledgments

Supported by NASA ACRES, with mentorship from Dr. Ana Tárano and Dr. Hannah Kerner (ASU SCAI). Based at the University of Hawaiʻi Maui College.
