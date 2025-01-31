# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

logger = logging.getLogger(__name__)


class HrPayslipLine(models.Model):
    _inherit = "hr.payslip.line"

    def _get_partner_id(self, credit_account):
        """
        Get partner_id of slip line to use in account_move_line
        """
        # use partner of salary rule or fallback on employee's address
        register_partner_id = self.salary_rule_id.register_id.partner_id
        partner_id = (
            register_partner_id.id or self.slip_id.employee_id.address_home_id.id
        )
        if credit_account:
            if (
                register_partner_id
                or self.salary_rule_id.account_credit.internal_type
                in ("receivable", "payable")
            ):
                return partner_id
        else:
            if (
                register_partner_id
                or self.salary_rule_id.account_debit.internal_type
                in ("receivable", "payable")
            ):
                return partner_id
        return False


class HrPayslip(models.Model):
    _inherit = "hr.payslip"

    date = fields.Date(
        "Date Account",
        states={"draft": [("readonly", False)]},
        readonly=True,
        help="Keep empty to use the period of the validation(Payslip) date.",
    )
    journal_id = fields.Many2one(
        "account.journal",
        "Salary Journal",
        readonly=True,
        required=True,
        states={"draft": [("readonly", False)]},
        default=lambda self: self.env["account.journal"].search(
            [("type", "=", "general")], limit=1
        ),
    )
    move_id = fields.Many2one(
        "account.move", "Accounting Entry", readonly=True, copy=False
    )

    @api.model
    def create(self, vals):
        if "journal_id" in self.env.context:
            vals["journal_id"] = self.env.context.get("journal_id")
        return super(HrPayslip, self).create(vals)

    @api.onchange("contract_id")
    def onchange_contract(self):
        super(HrPayslip, self).onchange_contract()
        self.journal_id = (
            self.contract_id.journal_id.id
            or (not self.contract_id and self.default_get(["journal_id"])["journal_id"])
            or self.journal_id
        )

    def action_payslip_cancel(self):
        for payslip in self:
            if not payslip.move_id.journal_id.restrict_mode_hash_table:
                payslip.move_id.with_context(force_delete=True).button_cancel()
                payslip.move_id.with_context(force_delete=True).unlink()
            else:
                payslip.move_id._reverse_moves()
                payslip.move_id = False
        return super(HrPayslip, self).action_payslip_cancel()

    def action_payslip_done(self):
        res = super(HrPayslip, self).action_payslip_done()

        for slip in self:
            line_ids = []
            debit_sum = 0.0
            credit_sum = 0.0
            date = slip.date or slip.date_to
            currency = (
                slip.company_id.currency_id or slip.journal_id.company_id.currency_id
            )

            name = _("Payslip of %s") % (slip.employee_id.name)
            move_dict = {
                "narration": name,
                "ref": slip.number,
                "journal_id": slip.journal_id.id,
                "date": date,
            }
            for line in slip.details_by_salary_rule_category:
                amount = currency.round(slip.credit_note and -line.total or line.total)
                if currency.is_zero(amount):
                    continue
                debit_account_id = line.salary_rule_id.account_debit.id
                credit_account_id = line.salary_rule_id.account_credit.id
                account_id = debit_account_id or credit_account_id
                analytic_salary_id = line.salary_rule_id.analytic_account_id.id

                tax_ids = False
                tax_tag_ids = False
                if line.salary_rule_id.tax_line_ids:
                    account_tax_ids = [
                        salary_rule_id.account_tax_id.id
                        for salary_rule_id in line.salary_rule_id.tax_line_ids
                    ]
                    tax_ids = [
                        (4, account_tax_id, 0) for account_tax_id in account_tax_ids
                    ]
                    tax_tag_ids = (
                        self.env["account.tax.repartition.line"]
                        .search(
                            [
                                ("invoice_tax_id", "in", account_tax_ids),
                                ("repartition_type", "=", "base"),
                            ]
                        )
                        .tag_ids
                    )

                tax_repartition_line_id = False
                if line.salary_rule_id.account_tax_id:
                    tax_repartition_line_id = (
                        self.env["account.tax.repartition.line"]
                        .search(
                            [
                                (
                                    "invoice_tax_id",
                                    "=",
                                    line.salary_rule_id.account_tax_id.id,
                                ),
                                ("account_id", "=", account_id),
                            ]
                        )
                        .id
                    )
                    tax_tag_ids = (
                        self.env["account.tax.repartition.line"]
                        .search(
                            [
                                (
                                    "invoice_tax_id",
                                    "=",
                                    line.salary_rule_id.account_tax_id.id,
                                ),
                                ("repartition_type", "=", "tax"),
                                ("account_id", "=", account_id),
                            ]
                        )
                        .tag_ids
                    )

                if debit_account_id:
                    debit_line = (
                        0,
                        0,
                        {
                            "name": line.name,
                            "partner_id": line._get_partner_id(credit_account=False)
                            or slip.employee_id.address_home_id.id,
                            "account_id": debit_account_id,
                            "journal_id": slip.journal_id.id,
                            "date": date,
                            "debit": amount > 0.0 and amount or 0.0,
                            "credit": amount < 0.0 and -amount or 0.0,
                            "analytic_account_id": analytic_salary_id
                            or slip.contract_id.analytic_account_id.id,
                            "tax_line_id": line.salary_rule_id.account_tax_id.id,
                            "tax_ids": tax_ids,
                            "tax_repartition_line_id": tax_repartition_line_id,
                            "tax_tag_ids": tax_tag_ids,
                        },
                    )
                    line_ids.append(debit_line)
                    debit_sum += debit_line[2]["debit"] - debit_line[2]["credit"]

                if credit_account_id:
                    credit_line = (
                        0,
                        0,
                        {
                            "name": line.name,
                            "partner_id": line._get_partner_id(credit_account=True)
                            or slip.employee_id.address_home_id.id,
                            "account_id": credit_account_id,
                            "journal_id": slip.journal_id.id,
                            "date": date,
                            "debit": amount < 0.0 and -amount or 0.0,
                            "credit": amount > 0.0 and amount or 0.0,
                            "analytic_account_id": analytic_salary_id
                            or slip.contract_id.analytic_account_id.id,
                            "tax_line_id": line.salary_rule_id.account_tax_id.id,
                            "tax_ids": tax_ids,
                            "tax_repartition_line_id": tax_repartition_line_id,
                            "tax_tag_ids": tax_tag_ids,
                        },
                    )
                    line_ids.append(credit_line)
                    credit_sum += credit_line[2]["credit"] - credit_line[2]["debit"]

            if currency.compare_amounts(credit_sum, debit_sum) == -1:
                acc_id = slip.journal_id.default_account_id.id
                if not acc_id:
                    raise UserError(
                        _(
                            'The Expense Journal "%s" has not properly '
                            "configured the Credit Account!"
                        )
                        % (slip.journal_id.name)
                    )
                adjust_credit = (
                    0,
                    0,
                    {
                        "name": _("Adjustment Entry"),
                        "partner_id": False,
                        "account_id": acc_id,
                        "journal_id": slip.journal_id.id,
                        "date": date,
                        "debit": 0.0,
                        "credit": currency.round(debit_sum - credit_sum),
                    },
                )
                line_ids.append(adjust_credit)

            elif currency.compare_amounts(debit_sum, credit_sum) == -1:
                acc_id = slip.journal_id.default_account_id.id
                if not acc_id:
                    raise UserError(
                        _(
                            'The Expense Journal "%s" has not properly '
                            "configured the Debit Account!"
                        )
                        % (slip.journal_id.name)
                    )
                adjust_debit = (
                    0,
                    0,
                    {
                        "name": _("Adjustment Entry"),
                        "partner_id": False,
                        "account_id": acc_id,
                        "journal_id": slip.journal_id.id,
                        "date": date,
                        "debit": currency.round(credit_sum - debit_sum),
                        "credit": 0.0,
                    },
                )
                line_ids.append(adjust_debit)
            if len(line_ids) > 0:
                move_dict["line_ids"] = line_ids
                move = self.env["account.move"].create(move_dict)
                slip.write({"move_id": move.id, "date": date})
                move.action_post()
            else:
                logger.warning(
                    f"Payslip {slip.number} did not generate any account move lines"
                )
        return res


class HrSalaryRule(models.Model):
    _inherit = "hr.salary.rule"

    analytic_account_id = fields.Many2one(
        "account.analytic.account", "Analytic Account"
    )
    account_tax_id = fields.Many2one("account.tax", "Tax")
    account_debit = fields.Many2one(
        "account.account", "Debit Account", domain=[("deprecated", "=", False)]
    )
    account_credit = fields.Many2one(
        "account.account", "Credit Account", domain=[("deprecated", "=", False)]
    )

    tax_base_id = fields.Many2one("hr.salary.rule", "Base")
    tax_line_ids = fields.One2many("hr.salary.rule", "tax_base_id", string="Tax lines")


class HrContract(models.Model):
    _inherit = "hr.contract"
    _description = "Employee Contract"

    analytic_account_id = fields.Many2one(
        "account.analytic.account", "Analytic Account"
    )
    journal_id = fields.Many2one("account.journal", "Salary Journal")


class HrPayslipRun(models.Model):
    _inherit = "hr.payslip.run"

    journal_id = fields.Many2one(
        "account.journal",
        "Salary Journal",
        states={"draft": [("readonly", False)]},
        readonly=True,
        required=True,
        default=lambda self: self.env["account.journal"].search(
            [("type", "=", "general")], limit=1
        ),
    )
