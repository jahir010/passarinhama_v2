import pdfkit
from io import BytesIO
from fastapi import UploadFile
from applications.earning.vendor_earning import PayoutTransaction
from app.utils.file_manager import save_file
UPLOAD_FOLDER = "payout_invoices"

WKHTML_PATH = r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"
async def generate_payout_pdf(transaction: PayoutTransaction) -> str:
    html = f"""
    <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; }}
                h2 {{ color: #2c3e50; }}
                p {{ font-size: 14px; }}
            </style>
        </head>
        <body>
            <h2>Payout Receipt</h2>
            <p><strong>Transaction ID:</strong> {transaction.transfer_id}</p>
            <p><strong>Amount:</strong> {transaction.amount}</p>
            <p><strong>Status:</strong> {transaction.status}</p>
            <p><strong>Date:</strong> {transaction.created_at.strftime("%Y-%m-%d %H:%M:%S")}</p>
        </body>
    </html>
    """

    config = pdfkit.configuration(wkhtmltopdf=WKHTML_PATH)
    pdf_bytes: bytes = pdfkit.from_string(html, False, configuration=config)

    pdf_file = UploadFile(
        filename=f"{transaction.transfer_id}.pdf",
        file=BytesIO(pdf_bytes)
    )

    file_url = await save_file(
        file=pdf_file,
        upload_to=UPLOAD_FOLDER,
        compress=False,
        allowed_extensions=["pdf"],
    )
    return file_url
