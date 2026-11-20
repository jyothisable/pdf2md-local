"""Source-backed Paddle guards, without loading Paddle or a vision model."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('source_guards', Path(__file__).resolve().parents[1] / 'pdf_to_md.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def block(label, text='', bbox=None):
    return SimpleNamespace(label=label, content=text, bbox=bbox or [10,10,100,30], image=None)


class SourceGuardTests(unittest.TestCase):
    def test_native_block_does_not_complete_a_page_end_sentence(self):
        src = ('The pipeline starts with a tokenizer that splits the text into words or word fragments. '
               'This section describes how the transformer layer is used in language tasks. Then each token')
        ocr = src + ' is used for natural language processing.'
        self.assertEqual(m.source_verified_prose(src, ocr), src)

    def test_native_prose_joins_line_wraps(self):
        src = 'The maximum likeli-\nhood estimate describes the parameters of a probability distribution.'
        ocr = 'The maximum likelihood estimate describes the parameters of a probability distribution.'
        self.assertEqual(m.source_verified_prose(src, ocr), ocr)

    def test_native_bad_encoding_or_unrelated_block_is_not_used(self):
        ocr = 'This is a normal paragraph with enough words to compare against another paragraph.'
        self.assertIsNone(m.source_verified_prose('unrelated text that describes a completely different topic from the current paragraph', ocr))
        self.assertIsNone(m.source_verified_prose(ocr+'\ufffd', ocr))
        self.assertIsNone(m.source_verified_prose(ocr, ocr+' $x_i$'))

    def test_composite_figure_retains_all_panels_as_one_image(self):
        blocks = [block('algorithm','diagram labels',[10,30,90,60]),
                  block('image','',[100,30,180,100]),
                  block('display_formula','$$bad{$$',[10,70,90,110]),
                  block('figure_title','Figure 3.2 A multi-panel diagram',[10,120,180,160])]
        result = {'height':200,'parsing_res_list':blocks}
        r = m.protect_paddle_result(result, lambda b:'', lambda b,p:'exact source crop')
        self.assertEqual(len(result['parsing_res_list']),2)
        self.assertEqual(result['parsing_res_list'][0].label,'image')
        self.assertEqual(r['composite_figures'][0]['panels'],3)
        self.assertEqual(r['composite_figures'][0]['bbox'],[10,30,180,110])

    def test_a_table_with_caption_is_not_flattened_to_an_image(self):
        blocks = [block('table','<table>readable</table>',[10,30,180,100]),
                  block('figure_title','Figure 3.2 Comparison table',[10,120,180,160])]
        self.assertEqual(m.composite_figure_groups(blocks,200),[])

    def test_prose_before_figure_is_not_swallowed(self):
        blocks = [block('image','',[10,10,90,20]),block('text','body paragraph',[10,25,90,30]),
                  block('image','',[10,40,90,70]),block('figure_title','Figure 1 Caption',[10,80,90,100])]
        self.assertEqual(m.composite_figure_groups(blocks,200),[])

    def test_bad_math_is_retried_then_preserved_as_source_image(self):
        b = block('display_formula','$$\\frac{x$$')
        result = {'height':200,'parsing_res_list':[b]}
        calls = []
        def retry(box,pad):
            calls.append(pad)
            return '$$\\frac{x$$'
        r = m.protect_paddle_result(result,lambda b:'',lambda b,p:'source pixels',retry)
        self.assertEqual(calls,[6,18])
        self.assertEqual(b.label,'image')
        self.assertEqual(b.image['img'],'source pixels')
        self.assertEqual(len(r['formula_images']),1)

    def test_successful_retry_stays_editable(self):
        b = block('display_formula','$$\\frac{x$$')
        r = m.protect_paddle_result({'height':200,'parsing_res_list':[b]},lambda b:'',lambda b,p:'crop',
                                    lambda box,pad:r'\frac{x}{y}=2')
        self.assertEqual(b.label,'display_formula')
        self.assertIn(r'\frac{x}{y}=2',b.content)
        self.assertEqual(len(r['formula_retries']),1)
        self.assertFalse(r['formula_images'])

    def test_valid_math_is_not_rewritten(self):
        b = block('display_formula','$$x^2+y^2=1$$')
        m.protect_paddle_result({'height':200,'parsing_res_list':[b]},lambda b:'',lambda b,p:'crop',
                                lambda *args:self.fail('unnecessary retry'))
        self.assertEqual(b.content,'$$x^2+y^2=1$$')

    def test_html_cell_line_breaks_survive_markdown_conversion(self):
        html = '<table><tr><th>Actions</th></tr><tr><td>Modify labels<br>Modify inputs</td></tr></table>'
        self.assertIn('Modify labels<br>Modify inputs',m.html_table_to_md(html))

    def test_only_escaped_bullet_breaks_are_unescaped(self):
        text = r'• Modify labels\n• Modify inputs $\nu$ C:\notes'
        self.assertEqual(m.table_cell_breaks(text),r'• Modify labels<br>• Modify inputs $\nu$ C:\notes')

    def test_nested_model_wrappers_are_removed_before_rendering(self):
        self.assertEqual(m.formula_body(r'$$\[x=y\]$$'),'x=y')
        self.assertIn('nested math delimiters',m.formula_retry_problems(r'x=1\)\(y=2'))

    def test_retry_rejects_an_invented_numerical_value(self):
        self.assertIn('numerical value absent from native source crop',
                      m.formula_retry_problems(r'x=0.5+2','source x = 1 + 2 is shown'))

    def test_broken_dense_annotation_is_not_guessed(self):
        b=block('display_formula',r'$$\overbrace{x}^{label$$')
        r=m.protect_paddle_result({'height':200,'parsing_res_list':[b]},lambda b:'',lambda b,p:'source',
                                  lambda *a:self.fail('an annotation should use exact source pixels'))
        self.assertEqual(b.label,'image')
        self.assertEqual(len(r['formula_images']),1)

    def test_renderable_extra_parenthesis_is_still_rejected(self):
        self.assertIn('unbalanced literal parentheses or brackets',
                      m.formula_retry_problems(r'f(y)=(1-sig[x]))^{1-y}'))
        self.assertEqual(m.formula_delimiter_problems(r'x\in(0,1]'),[])
        self.assertEqual(m.formula_delimiter_problems(r'\left(x\right.'),[])

    def test_overbar_alias_uses_a_supported_math_macro(self):
        self.assertEqual(m.formula_body(r'\overbar{x}'),r'\overline{x}')

    def test_silent_matrix_dimension_error_triggers_recovery(self):
        b=block('display_formula',r'$$\begin{bmatrix}1\\1\\1\\1\end{bmatrix}$$')
        native='1  1  1'
        r=m.protect_paddle_result({'height':200,'parsing_res_list':[b]},lambda b:native,lambda b,p:'source',
                                  lambda *a:r'\begin{bmatrix}1\\1\\1\end{bmatrix}')
        self.assertEqual(len(r['formula_retries']),1)
        self.assertEqual(b.content.count('1'),3)

    def test_retry_must_not_omit_a_source_number(self):
        self.assertIn('numerical value omitted from native source crop',
                      m.formula_retry_problems('x=2','source x = 1 + 2 is shown'))

    def test_bad_inline_math_can_use_plain_native_prose(self):
        src=('Digital images are represented as matrices. Each element represents the brightness '
             'at a specific pixel position (x, y coordinate) of the image.')
        b=block('text',src.replace('(x, y coordinate)', '$(x, y$ coordinate $'))
        r=m.protect_paddle_result({'height':200,'parsing_res_list':[b]},lambda b:src,lambda b,p:'source')
        self.assertEqual(b.content,src)
        self.assertEqual(len(r['inline_restored']),1)

    def test_unrecoverable_inline_math_keeps_exact_paragraph_crop(self):
        b=block('text',r'The probability is $\frac{x$ and needs a source check.')
        r=m.protect_paddle_result({'height':200,'parsing_res_list':[b]},lambda b:'',lambda b,p:'source')
        self.assertEqual(b.label,'image')
        self.assertEqual(len(r['inline_images']),1)

    def test_incomplete_api_answer_cannot_be_accepted_as_an_equation(self):
        payload={'choices':[{'finish_reason':'length','message':{'content':'x=2'}}]}
        with patch.object(m.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaises(ValueError):
                m.openai_chat('http://localhost:1','local',None,'Formula Recognition:',1,require_complete=True)

    def test_worker_changes_invalidate_conversion_checkpoints(self):
        with tempfile.TemporaryDirectory() as d:
            pdf=Path(d)/'source.pdf';pdf.write_bytes(b'source')
            worker=Path(d)/'worker.py';worker.write_text('version one')
            with patch.object(m,'WORKER',worker):
                before=m.run_key(pdf,2.5)
                worker.write_text('version two')
                self.assertNotEqual(before,m.run_key(pdf,2.5))

    def test_rectangular_determinant_bars_use_source_not_a_guessed_operator(self):
        tex=r'\left|\begin{array}{ccc}1&2&3\\4&5&6\end{array}\right|'
        self.assertIn('ambiguous rectangular matrix inside determinant bars',m.formula_delimiter_problems(tex))
        square=r'\left|\begin{array}{cc}1&2\\3&4\end{array}\right|'
        self.assertEqual(m.formula_delimiter_problems(square),[])

    def test_source_typos_are_not_rewritten_using_model_knowledge(self):
        self.assertEqual(m.native_matrix_problems(r'\begin{bmatrix}x_0\\x_1\\x_1\end{bmatrix}','x0 x1 x1'),[])

    def test_a_probability_p_cannot_silently_become_phi(self):
        native='p(E1 or E2) = p(E1) + p(E2)'
        self.assertIn('Greek symbol absent from native source crop',
                      m.native_greek_problems(r'\phi(E_1)+\phi(E_2)',native))
        self.assertEqual(m.native_greek_problems('p(E_1)+p(E_2)',native),[])
        self.assertEqual(m.native_greek_problems(r'\phi(x)=\phi(y)','φ(x) = φ(y) for the two inputs'),[])

    def test_ambiguous_mathematical_font_encoding_is_not_used_as_greek_evidence(self):
        self.assertEqual(m.native_greek_problems(r'\lambda+x','휆 + x is shown in this source'),[])

    def test_bookmarked_repeated_margin_title_is_not_a_second_section(self):
        b=block('paragraph_title','2.1 Topic',[10,10,600,30])
        result={'height':1000,'parsing_res_list':[b]}
        r=m.protect_paddle_result(result,lambda b:'',lambda b,p:'source',page_no=8,
            outline=[{'level':2,'title':'2.1 Topic','page':5}],repeated_margin=lambda text:True)
        self.assertEqual(result['parsing_res_list'],[])
        self.assertEqual(len(r['running_headers']),1)

    def test_real_title_on_its_bookmarked_page_is_preserved(self):
        b=block('paragraph_title','2.1 Topic',[10,10,600,30])
        result={'height':1000,'parsing_res_list':[b]}
        m.protect_paddle_result(result,lambda b:'',lambda b,p:'source',page_no=5,
            outline=[{'level':2,'title':'2.1 Topic','page':5}],repeated_margin=lambda text:True)
        self.assertEqual(result['parsing_res_list'],[b])

    def test_proportionality_cannot_be_replaced_by_infinity(self):
        source='p(X) = ∝ e to the power of X'
        self.assertIn('mathematical operator omitted or substituted relative to native source crop',
                      m.native_operator_problems(r'p(X)=\infty e^X',source))

    def test_retry_variable_case_and_missing_terms_are_checked(self):
        source='p(X) = p(Y) + p(Z)'
        self.assertEqual(m.native_variable_problems('p(X)=p(Y)+p(Z)',source),[])
        self.assertIn('variable letters or multiplicities differ from native source crop',
                      m.native_variable_problems('P(X)=p(Y)+p(Z)',source))
        self.assertIn('variable letters or multiplicities differ from native source crop',
                      m.native_variable_problems('p(X)=p(Y)',source))

    def test_uncertain_native_operator_uses_source_not_another_guess(self):
        b=block('display_formula',r'$$p(X)=\infty e^X$$')
        report=m.protect_paddle_result({'height':200,'parsing_res_list':[b]},lambda b:'p(X) = ∝ e to the power of X',
            lambda b,p:'source',lambda *a:self.fail('uncertain operators must retain source pixels'))
        self.assertEqual(b.label,'image')
        self.assertEqual(len(report['formula_images']),1)


if __name__ == '__main__':
    unittest.main()
