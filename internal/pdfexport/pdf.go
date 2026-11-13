// Package pdfexport draws the logbook as a PDF that looks like a green
// military logbook: a green cover, then lined pages with a margin rule and
// typewriter-style (Courier) text.
//
// It uses the pure-Go fpdf library and only its built-in fonts, so the
// container stays small. Those fonts cover Western European characters;
// anything else (e.g. emoji, Chinese) prints as "?". The JSON export always
// keeps the exact original text.
package pdfexport

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/go-pdf/fpdf"
	"github.com/munozajj/caplog/internal/logbook"
)

// Colours (R, G, B).
var (
	coverGreen = [3]int{46, 74, 46}    // olive drab cover
	ruleGreen  = [3]int{176, 204, 176} // faint page lines
	marginRed  = [3]int{200, 90, 90}   // left margin rule
	ink        = [3]int{30, 40, 30}
	paper      = [3]int{246, 248, 236}
)

const (
	lineH   = 6.0  // mm between ruled lines; text is aligned to them
	topY    = 24.0 // first ruled line
	leftX   = 28.0 // margin rule position
	rightX  = 200.0
	bottomY = 280.0
)

// Each is a function that feeds every record to fn, in order.
type Each func(fn func(logbook.Record) error) error

// Write renders the whole log. exportRec is the "export" note that was just
// appended; it is the last record included.
func Write(w io.Writer, title string, exportRec logbook.Record, each Each) error {
	pdf := fpdf.New("P", "mm", "A4", "")
	pdf.SetTitle(title, true)
	pdf.SetCreator("CapLog", true)
	pdf.SetAutoPageBreak(false, 0)
	tr := pdf.UnicodeTranslatorFromDescriptor("") // UTF-8 -> the PDF's built-in font encoding
	clean := func(s string) string { return tr(s) }

	// ---- cover ----
	pdf.AddPage()
	fill(pdf, coverGreen)
	pdf.Rect(0, 0, 210, 297, "F")
	// stitched spine
	pdf.SetDrawColor(30, 50, 30)
	pdf.SetLineWidth(1.2)
	pdf.Line(14, 0, 14, 297)
	// label plate
	fill(pdf, paper)
	pdf.SetDrawColor(20, 30, 20)
	pdf.SetLineWidth(0.6)
	pdf.Rect(40, 70, 130, 70, "FD")
	text(pdf, ink)
	pdf.SetFont("Courier", "B", 26)
	pdf.SetXY(40, 80)
	pdf.CellFormat(130, 12, "LOGBOOK", "", 1, "C", false, 0, "")
	pdf.SetFont("Courier", "B", 15)
	pdf.SetXY(46, 98)
	pdf.MultiCell(118, 7, clean(title), "", "C", false)
	pdf.SetFont("Courier", "", 9)
	pdf.SetXY(40, 126)
	pdf.CellFormat(130, 5, fmt.Sprintf("Records #0-#%d", exportRec.Seq), "", 1, "C", false, 0, "")
	pdf.SetX(40)
	pdf.CellFormat(130, 5, "Exported "+fmtTime(exportRec.CreatedAt), "", 1, "C", false, 0, "")
	pdf.SetTextColor(220, 225, 200)
	pdf.SetFont("Courier", "", 8)
	pdf.SetXY(20, 255)
	pdf.MultiCell(170, 4, "Last record fingerprint (SHA-256):\n"+exportRec.Hash+
		"\n\nEvery record is permanent and hash-chained. Verify the matching JSON export with \"caplog verify -file\".", "", "C", false)

	// ---- lined pages ----
	page := 0
	y := bottomY // forces a new page for the first record
	newPage := func() {
		pdf.AddPage()
		page++
		fill(pdf, paper)
		pdf.Rect(0, 0, 210, 297, "F")
		draw(pdf, ruleGreen)
		pdf.SetLineWidth(0.2)
		for ly := topY; ly <= bottomY; ly += lineH {
			pdf.Line(10, ly, rightX, ly)
		}
		draw(pdf, marginRed)
		pdf.Line(leftX-2, 10, leftX-2, 287)
		text(pdf, ink)
		pdf.SetFont("Courier", "B", 9)
		pdf.SetXY(leftX, 12)
		pdf.CellFormat(rightX-leftX, 6, clean(trunc(title, 60)), "", 0, "L", false, 0, "")
		pdf.SetFont("Courier", "", 8)
		pdf.SetXY(leftX, 287)
		pdf.CellFormat(rightX-leftX, 5, fmt.Sprintf("page %d", page), "", 0, "R", false, 0, "")
		y = topY
	}
	// writeLines prints text on consecutive ruled lines, wrapping as needed.
	writeLines := func(style string, size float64, s string, indent float64) {
		pdf.SetFont("Courier", style, size)
		for _, ln := range pdf.SplitLines([]byte(clean(s)), rightX-leftX-indent) {
			if y+lineH > bottomY {
				newPage()
				pdf.SetFont("Courier", style, size)
			}
			pdf.SetXY(leftX+indent, y-lineH+1.2)
			pdf.CellFormat(rightX-leftX-indent, lineH-1, string(ln), "", 0, "L", false, 0, "")
			y += lineH
		}
	}

	err := each(func(r logbook.Record) error {
		if y+3*lineH > bottomY { // keep a record's header with at least 2 lines
			newPage()
		}
		head := fmt.Sprintf("#%04d  %s", r.Seq, fmtTime(r.CreatedAt))
		switch r.Kind {
		case logbook.KindGenesis:
			writeLines("B", 10, head+"  LOGBOOK OPENED", 0)
			writeLines("", 10, "Title: "+r.Body, 4)
		case logbook.KindEntry:
			writeLines("B", 10, head+"  "+r.LastName+", "+r.FirstName, 0)
			writeLines("", 10, r.Body, 4)
		case logbook.KindImported:
			var m logbook.ImportedMeta
			_ = json.Unmarshal([]byte(r.Meta), &m)
			who := strings.TrimSpace(r.LastName + ", " + r.FirstName)
			if r.FirstName == "" && r.LastName == "" {
				who = "SYSTEM (" + m.SourceKind + ")"
			}
			writeLines("B", 10, head+"  "+who+"  [IMPORTED]", 0)
			writeLines("I", 8, fmt.Sprintf("from \"%s\" #%d, %s", m.SourceTitle, m.SourceSeq, fmtTime(m.SourceCreatedAt)), 4)
			writeLines("", 10, r.Body, 4)
		default: // export / import notes
			writeLines("B", 10, head+"  SYSTEM: "+strings.ToUpper(r.Kind), 0)
			writeLines("I", 9, r.Body, 4)
		}
		y += lineH // blank ruled line between records (keeps text on the lines)
		return nil
	})
	if err != nil {
		return err
	}
	return pdf.Output(w)
}

func fmtTime(s string) string {
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return s
	}
	return t.UTC().Format("2006-01-02 15:04:05 UTC")
}

func trunc(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-3]) + "..."
}

func fill(p *fpdf.Fpdf, c [3]int) { p.SetFillColor(c[0], c[1], c[2]) }
func draw(p *fpdf.Fpdf, c [3]int) { p.SetDrawColor(c[0], c[1], c[2]) }
func text(p *fpdf.Fpdf, c [3]int) { p.SetTextColor(c[0], c[1], c[2]) }
