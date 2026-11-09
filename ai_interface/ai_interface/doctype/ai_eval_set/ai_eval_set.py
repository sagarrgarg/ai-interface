import frappe
from frappe.model.document import Document


class AIEvalSet(Document):
	def validate(self):
		if self.target_type == "Callable" and self.target:
			from ai_interface.services.evals import check_target

			check_target(self.target)
