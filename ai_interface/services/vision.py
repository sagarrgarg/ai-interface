import os

import frappe
from frappe import _

from ai_interface.services.ai_client import call_ai


def extract(
	file_url: str,
	prompt: str = "Extract all text and structured data from this document.",
	*,
	provider: str | None = None,
	model: str | None = None,
	calling_app: str = "",
	sync: bool = False,
	max_tokens: int | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	module: str | None = None,
	action: str | None = None,
	**kwargs,
) -> str:
	"""Extract information from an image or PDF using vision.

	Accepts Frappe file URLs. PDFs are converted to images (page limit from AI Settings).
	Returns AI Call Log name (async) or extracted text (sync).
	"""
	file_urls = _resolve_file_urls(file_url)

	try:
		return call_ai(
			prompt=prompt,
			images=file_urls,
			provider=provider,
			model=model,
			calling_app=calling_app,
			function_type="Vision",
			sync=sync,
			max_tokens=max_tokens,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			module=module,
			action=action,
			**kwargs,
		)
	finally:
		_cleanup_temp_images(file_urls)


def _resolve_file_urls(file_url: str) -> list[str]:
	"""If PDF, convert to images. Otherwise return as-is."""
	file_path = _get_file_path(file_url)

	if file_path.lower().endswith(".pdf"):
		return _pdf_to_image_urls(file_path)

	return [file_url]


def _get_file_path(file_url: str) -> str:
	if file_url.startswith("/files/") or file_url.startswith("/private/files/"):
		return frappe.get_site_path(file_url.lstrip("/"))
	return frappe.get_site_path("public", file_url.lstrip("/"))


def _pdf_to_image_urls(pdf_path: str) -> list[str]:
	"""Convert PDF pages to temporary image files, return their Frappe file URLs."""
	from pdf2image import convert_from_path

	settings = frappe.get_single("AI Settings")
	max_pages = settings.max_pdf_pages or 10

	images = convert_from_path(pdf_path, last_page=max_pages)

	temp_dir = frappe.get_site_path("private", "files", "ai_temp")
	os.makedirs(temp_dir, exist_ok=True)

	image_urls = []
	for i, img in enumerate(images):
		filename = f"ai_pdf_page_{frappe.generate_hash(length=8)}_{i}.png"
		img_path = os.path.join(temp_dir, filename)
		img.save(img_path, "PNG")
		image_urls.append(f"/private/files/ai_temp/{filename}")

	return image_urls


def _cleanup_temp_images(file_urls: list[str]):
	"""Remove temporary PDF-converted images."""
	for url in file_urls:
		if "/ai_temp/" not in url:
			continue
		try:
			path = frappe.get_site_path(url.lstrip("/"))
			if os.path.exists(path):
				os.unlink(path)
		except OSError:
			pass
