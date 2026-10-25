# SMR_Project

SMR(소형모듈원자로) 공개 인허가 문서에서 도면(Figure)을 모아 내부 연구용 도면 DB를 만드는 파이프라인.

현재는 파일럿 단계. 문서 5건으로 "문서번호 → 다운로드 → 도면 크롭 + 메타데이터"까지 끝까지 도는 것을 확인함.

## 설치

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # ADAMS_API_KEY, SMR_CONTACT_EMAIL 입력
```

- Python 3.11 이상 (3.14에서 확인)
- `ADAMS_API_KEY`: [NRC ADAMS API 개발자 포털](https://adams-api-developer.nrc.gov)에서 구독 키 발급
- `SMR_CONTACT_EMAIL`: 다운로드 요청의 User-Agent에 들어가는 연락처. 본인 메일로

## 사용법

```bash
# 1) 문서 받기
python fetch.py --ml ML25099A239                    # ADAMS 번호. 문서 약칭 자동 생성
python fetch.py nuscale-us600-fsar-ch1-r4 --ml ML20036D417   # 약칭 직접 지정
python fetch.py bwrx300-gda-psr-ch1-rb --url https://.../11.pdf
python fetch.py <약칭> --import data/inbox/X.pdf <원래 URL>  # 403 등으로 막혀서 직접 받은 경우

# 2) 도면 추출 (이름 생략 시 data/docs 전부)
python extract.py nuscale-us600-fsar-ch1-r4

# 테스트
python -m pytest tests
```

`--url`, `--import`로 받은 문서는 `data/docs/<약칭>/doc.json`을 직접 만들어야 함. 형식은 `{"reactor": ..., "title": ..., "adams_no": ...}`.

## 결과 구조

`data/`는 git에 포함하지 않음.

```
data/
├── docs/<문서 약칭>/
│   ├── doc.json              노형, 문서명, ADAMS 번호, 도켓
│   ├── adams.json, adams.txt ADAMS 응답 원문, 본문 텍스트
│   ├── <sha256>.pdf          원본 (읽기 전용, 수정 금지)
│   ├── <sha256>.json         출처: URL, 받은 시각, 응답 헤더 (받을 때마다 누적)
│   ├── figures/<figure_id>.png / .json
│   └── figures.csv
├── inbox/                    직접 받은 PDF 넣는 곳
└── index.csv                 전체 도면 목록
```

- 문서 약칭: `{노형}-{문서종류}-{장}-{개정판}`. 예: `nuscale-us460-fsar-ch1-r2`
- figure_id: `{문서 약칭}-fig-{그림번호}`. 여러 장짜리 그림은 `-s{장번호}`를 붙임. 예: `xe100-longmott-cpa-psar-fig-2.3-29-s1`

도면 json의 주요 필드:

| 필드 | 내용 |
|---|---|
| `pdf_page` | 1부터 세는 PDF 쪽 번호 (브라우저 `#page=N`과 같음) |
| `printed_page` / `printed_page_lof` | 문서에 인쇄된 쪽 표기 / 그림 목록에 적힌 쪽 표기 |
| `caption`, `lof_title` | 본문 캡션 / 그림 목록의 제목 |
| `status`, `reason` | `public` / `withheld` / `unmapped`, 비공개 판정 이유 |
| `crop_method` | `vector`, `raster`, `raster+vector`, `page` |
| `bbox_pt_unrotated` | 크롭 영역 (캡션·범례·주석 포함) |
| `figure_only_bbox_pt_unrotated` | 캡션을 뺀 그림 영역 |
| `phash` | perceptual hash (중복 묶기용) |
| `figure_type` | 도면 유형. 아직 비어 있음 (AI 분류 예정) |

## 처리 방식

1. **그림 목록**: 앞부분의 List of Figures에서 그림 번호, 제목, 인쇄 쪽을 읽음
2. **캡션 매핑**: 본문에서 줄 맨 앞의 `Figure X.Y-Z:`를 찾아 PDF 쪽에 연결. 목차류 쪽은 제외
3. **비공개 판정**:
   - `Withheld` 문구 → `text:Withheld`
   - 기밀 괄호 `[[ ]]`만 남은 쪽 → `proprietary-brackets`
   - 보안 안내문 → `notice:…`
   - 내용 없음 → `empty-body`
4. **크롭**: 200dpi. 드로잉·이미지 경계에 캡션, 범례, 주석을 붙여 자름. 다음은 뺌:
   - 머리글·바닥글, 반복 로고
   - 여백의 개정 표시줄
   - 같은 쪽의 다른 그림
5. **회전 페이지**: 메모리에서만 회전을 풀고 처리한 뒤 이미지를 다시 돌림. 원본 PDF는 건드리지 않음

## 수집 규칙

- 같은 호스트 요청 간격 3초 이상, 동시 접속 1개
- 실패 시 지수 백오프로 최대 3회 재시도. 429와 5xx만 재시도하고, 403·404는 바로 기록하고 넘어감
- robots.txt 준수
- 403 같은 접근 차단은 우회하지 않음 (프록시, 브라우저 위장 금지). 막히면 직접 받아 `--import`로 등록
- 원본 PDF는 수정하지 않음. 모든 가공은 파생 파일로
- API 키는 `.env`에만 두고 코드·로그에 남기지 않음
- 수집 자료는 내부 연구용이고 외부에 올리지 않음. 개발사 문서는 재배포에 허가가 필요함

## 파일럿 결과

| 문서 | 쪽 | 그림 | 공개 | 비공개 | 매핑 실패 |
|---|---|---|---|---|---|
| NuScale US600 DCA FSAR Rev.4 Ch.1 (ML20036D417) | 350 | 42 | 16 | 26 | 0 |
| NuScale US460 SDAA FSAR Rev.2 Ch.1 (ML25099A239) | 321 | 35 | 14 | 21 | 0 |
| BWRX-300 UK GDA PSR Ch.1 Rev.B | 35 | 4 | 4 | 0 | 0 |
| Xe-100 Long Mott CPA PSAR (ML25090A061) | 1,944 | 609 | 585 | 24 | 0 |
| Xe-100 PDC 토피컬 보고서 (ML22195A260) | 82 | 0 | – | – | – |

- 인쇄 쪽 표기는 모두 그림 목록과 일치함
- US600 1장의 비공개 26개는 기대값과 같음
- Long Mott(1,944쪽) 추출에 약 2분 30초, 메모리 약 1GB
- PDC 토피컬 보고서에는 그림이 없음

## 알려진 한계

- 그림 목록이 없는 문서(토피컬 보고서, RAI 답변, 발표 자료 등)에서는 도면을 못 뽑음
- 비공개 안내문이 글자가 아니라 이미지로 들어간 경우는 걸러내지 못함. 예: Long Mott 7.3.1-9
- 도면 유형 분류, 중복 묶기, 저작권 고지 추출은 아직 없음
- 문서별로 명령을 실행하는 방식임. 대량 처리용 상태 관리(진행 기록, 재시작)는 아직 없음

## 다음 작업

- 도면 저장 포맷 기준이 정해지면 반영 예정. 바뀔 수 있는 것:
  - 함께 저장할 파일
  - 이미지에 같이 담을 요소
  - 본문에서 찾아 붙일 메타데이터
- ADAMS 검색 API로 도켓별 문서 목록 수집
- 크롭 이미지만 보내는 Claude API 도면 유형 분류
