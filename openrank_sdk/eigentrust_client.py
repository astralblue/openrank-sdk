from typing import List, Tuple, TypedDict
import numbers
import time
import json
import urllib3
import logging
import csv
import os
import gzip
import io

class IJV(TypedDict):
  i: str
  j: str
  v: float

IJV_CSV_HEADERS = ['i', 'j', 'v']
class IV(TypedDict):
  i: str
  v: float

IV_CSV_HEADERS = ['i', 'v']
class Score(TypedDict):
  i: str
  v: float

SCORE_CSV_HEADERS = ['i', 'v']

DEFAULT_EIGENTRUST_ALPHA: float = 0.5
DEFAULT_EIGENTRUST_EPSILON: float = 1.0
DEFAULT_EIGENTRUST_MAX_ITER: int = 50
DEFAULT_EIGENTRUST_FLAT_TAIL: int = 2
DEFAULT_EIGENTRUST_HOST_URL: str = "https://openrank-sdk-api.k3l.io"
DEFAULT_EIGENTRUST_TIMEOUT_MS: int = 15 * 60 * 1000

class EigenTrust:
  def __init__(self, **kwargs):
    self.alpha = kwargs.get('alpha') if isinstance(kwargs.get('alpha'), numbers.Number) else DEFAULT_EIGENTRUST_ALPHA
    self.epsilon = kwargs.get('epsilon') if isinstance(kwargs.get('epsilon'), numbers.Number) else DEFAULT_EIGENTRUST_EPSILON
    self.max_iter = kwargs.get('max_iter') if isinstance(kwargs.get('max_iter'), numbers.Number) else DEFAULT_EIGENTRUST_MAX_ITER
    self.flat_tail = kwargs.get('flat_tail') if isinstance(kwargs.get('flat_tail'), numbers.Number) else DEFAULT_EIGENTRUST_FLAT_TAIL
    self.go_eigentrust_host_url = kwargs.get('host_url') if isinstance(kwargs.get('host_url'), str) else DEFAULT_EIGENTRUST_HOST_URL
    self.go_eigentrust_timeout_ms = kwargs.get('timeout') if isinstance(kwargs.get('timeout'), numbers.Number) else DEFAULT_EIGENTRUST_TIMEOUT_MS
    self.api_key = kwargs.get('api_key') if isinstance(kwargs.get('api_key'), str) else ''
    self.http = urllib3.PoolManager()
    logging.basicConfig(level=logging.INFO)

  ## existing methods
  def run_eigentrust(self, localtrust: List[IJV], pretrust: List[IV]=None) -> List[Score]:
    start_time = time.perf_counter()

    lt = []
    for l in localtrust:
      if l['v'] <= 0.0:
        logging.warn(f"v cannot be less than or equal to 0, skipping this entry: {l}")
      elif l['i'] == l['j']:
        logging.warn(f"i and j cannot be same, skipping this entry: {l}")
      else:
        lt.append(l)
    localtrust = lt

    addresses = set()
    for l in localtrust:
      addresses.add(l["i"])
      addresses.add(l["j"])
    if len(addresses) <= 0:
      print(f"No edges found for {addresses}")
      return []

    addr_to_int_map = {}
    int_to_addr_map = {}
    for idx, addr in enumerate(addresses):
      addr_to_int_map[addr] = idx
      int_to_addr_map[idx] = addr

    if not pretrust:
      pt_len = len(addresses)
      logging.debug(f"generating pretrust from localtrust with equally weighted pretrusted value")
      pretrust = [{'i': addr_to_int_map[addr], 'v': 1/pt_len} for addr in addresses]
    else:
      pt = []
      for p in pretrust:
        if p['v'] <= 0.0:
          logging.warn(f"v cannot be less than or equal to 0, skipping this entry: {p}")
        elif not p['i'] in addresses:
          logging.warn(f"i entry not found in localtrust, skipping this entry: {p}")
        else:
          pt.append(p)
      pretrust = pt
      pretrust = [{'i': addr_to_int_map[p['i']], 'v': p['v']} for p in pretrust]

    logging.debug(f"generating localtrust with {len(addresses)} addresses")
    localtrust = [{'i': addr_to_int_map[l['i']],
                  'j': addr_to_int_map[l['j']],
                  'v': l['v']} for l in localtrust]
    max_id = len(addresses)

    logging.debug("calling go_eigentrust")
    i_scores = self._send_go_eigentrust_req(pretrust=pretrust,
                                            max_pt_id=max_id,
                                            localtrust=localtrust,
                                            max_lt_id=max_id)

    addr_scores = [{'i': int_to_addr_map[i_score['i']], 'v': i_score['v']} for i_score in i_scores]
    logging.info(f"eigentrust compute took {time.perf_counter() - start_time} secs ")
    addr_scores.sort(key=lambda x: x['v'], reverse=True)
    return addr_scores

  def run_eigentrust_from_csv(self, localtrust_filename: str, pretrust_filename: str = None) -> List[Score]:
    localtrust = []
    with open(localtrust_filename, "r") as f:
      reader = csv.reader(f, delimiter=",")
      for i, line in enumerate(reader):
        i, j, v = line[0], line[1], line[2]
        # is header
        if not v.isnumeric():
          continue
        localtrust.append({'i': str(i), 'j': str(j), 'v': float(v)})

    pretrust = None
    if pretrust_filename:
      pretrust = []
      with open(pretrust_filename, "r") as f:
        reader = csv.reader(f, delimiter=",")
        for i, line in enumerate(reader):
          i, v = line[0], line[1]
          # is header
          if not v.isnumeric():
            continue
          pretrust.append({'i': str(i), 'v': float(v)})

    return self.run_eigentrust(localtrust, pretrust)

  def _send_go_eigentrust_req(
    self,
    pretrust: list[dict],
    max_pt_id: int,
    localtrust: list[dict],
    max_lt_id: int,
  ):
    req = {
      "pretrust": {
        "scheme": 'inline',
        "size": int(max_pt_id)+1, #np.int64 doesn't serialize; cast to int
        "entries": pretrust,
      },
      "localTrust": {
        "scheme": 'inline',
        "size": int(max_lt_id)+1, #np.int64 doesn't serialize; cast to int
        "entries": localtrust,
      },
      "alpha": self.alpha,
      "epsilon": self.epsilon,
      "max_iterations": self.max_iter,
      "flatTail": self.flat_tail,
    }

    start_time = time.perf_counter()
    try:
      encoded_data = json.dumps(req).encode('utf-8')

      response = self.http.request('POST', f"{self.go_eigentrust_host_url}/basic/v1/compute",
                  headers={
                          'Accept': 'application/json',
                          'Content-Type': 'application/json',
                          'API-Key': self.api_key,
                          },
                  body=encoded_data,
                  timeout=self.go_eigentrust_timeout_ms,
                  )
      resp_dict = json.loads(response.data.decode('utf-8'))

      if response.status != 200:
        logging.error(f"Server error: {response.status}:{resp_dict} {resp_dict}")
        raise {
          "statusCode": response.status,
          "body": str(resp_dict)
      }

      return resp_dict["entries"]
    except Exception as e:
      logging.error('error while sending a request to go-eigentrust', e)
    logging.debug(f"go-eigentrust took {time.perf_counter() - start_time} secs ")

  def export_scores_to_csv(self, scores: List[Score], filepath: str, headers: List[str]):
    with open(filepath, 'w', newline='') as csvfile:
      writer = csv.writer(csvfile, delimiter=',')
      for line in scores:
        item = []
        for h in headers:
          item.append(line[h])
        writer.writerow(item)

  def export_csv_to_dune(
    self,
    filepath: str,
    headers: List[str],
    tablename: str,
    description: str,
    is_private: bool,
    api_key: str,
  ):
    csv_header = ""
    for h in headers:
      csv_header += f"{h},"
    lines = [csv_header]
    with open(filepath, "r") as f:
      reader = csv.reader(f, delimiter=',')
      for _, line in enumerate(reader):
        header = ""
        for l in line:
          header += f'{l},'
        lines.append(header)
    data = '\n'.join(lines)
    req = {
      "data": data,
      "description": description,
      "table_name": tablename,
      "is_private": is_private,
    }

    start_time = time.perf_counter()
    try:
      encoded_data = json.dumps(req)

      response = self.http.request('POST', "https://api.dune.com/api/v1/table/upload/csv",
                  headers={
                          'Accept': 'application/json',
                          'Content-Type': 'application/json',
                          'X-DUNE-API-KEY': api_key,
                          },
                  body=encoded_data,
                  # timeout=30 * 1000,
                  )
      resp_dict = json.loads(response.data.decode('utf-8'))

      if response.status != 200:
        logging.error(f"Server error: {response.status}:{resp_dict} {resp_dict}")
        raise {
          "statusCode": response.status,
          "body": str(resp_dict)
      }

      return resp_dict
    except Exception as e:
      logging.error('error while sending a request to dune-upload-csv', e)
    logging.debug(f"dune-upload-csv took {time.perf_counter() - start_time} secs ")

  # New methods to interact with the backend server
  def _upload_csv(self, data: List[dict], headers: List[str], endpoint: str, overwrite: bool) -> str:
    # Create an in-memory file-like object for the CSV data
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)

    # Write CSV headers
    writer.writerow(headers)

    # Write CSV rows
    for item in data:
      writer.writerow(item.values())

    # # Compress the CSV data using gzip
    # compressed = io.BytesIO()
    # with gzip.GzipFile(fileobj=compressed, mode='wb') as gz:
    #   gz.write(csv_buffer.getvalue().encode('utf-8'))

    # Send the compressed data to the server
    response = self.http.request(
      'POST',
      f'{self.go_eigentrust_host_url}/upload/{endpoint}?overwrite={overwrite}',
      headers={'Content-Type': 'application/gzip'},
      # body=compressed.getvalue()
      body=csv_buffer.getvalue().encode('utf-8'),
    )

    if response.status != 200:
      raise Exception(f"Failed to upload CSV: {response.data.decode('utf-8')}")

    return f'{self.go_eigentrust_host_url}/download/{endpoint}'

  def _download_csv(self, endpoint: str) -> List[dict]:
    response = self.http.request(
      'GET',
      f'{self.go_eigentrust_host_url}/download/{endpoint}',
      headers={'Accept': 'application/gzip'}
    )
    if response.status != 200:
      raise Exception(f"Failed to download CSV: {response.data.decode('utf-8')}")
    data = response.data.decode('utf-8').splitlines()
    reader = csv.DictReader(data)
    return [row for row in reader]

  def _convert_to_ijv(self, data: List[dict]) -> List[IJV]:
    return [{'i': row['i'], 'j': row['j'], 'v': float(row['v'])} for row in data]

  def _convert_to_iv(self, data: List[dict]) -> List[IV]:
    return [{'i': row['i'], 'v': float(row['v'])} for row in data]

  def _convert_to_score(self, data: List[dict]) -> List[Score]:
    return [{'i': row['i'], 'v': float(row['v'])} for row in data]

  def run_eigentrust_from_id(self, localtrust_id: str, pretrust_id: str = None) -> Tuple[List[Score], str]:
    data = {
        'localtrust_id': localtrust_id,
        'alpha': self.alpha,
        'epsilon': self.epsilon,
        'max_iterations': self.max_iter,
        'flatTail': self.flat_tail,
    }
    if pretrust_id:
      data['pretrust_id'] = pretrust_id

    response = self.http.request(
      'POST',
      f'{self.go_eigentrust_host_url}/compute_from_id',
      headers={'Content-Type': 'application/json'},
      body=json.dumps(data).encode('utf-8')
    )

    if response.status != 200:
      raise Exception(f"Failed to run eigentrust: {response.data.decode('utf-8')}")

    resp_dict = json.loads(response.data.decode('utf-8'))
    scores = [Score(i=item['i'], v=item['v']) for item in resp_dict['scores']]
    return scores

  def run_and_publish_eigentrust_from_id(self, id: str, localtrust_id: str, pretrust_id: str = None, **kwargs) -> Tuple[List[Score], str]:
    scores = self.run_eigentrust_from_id(localtrust_id, pretrust_id)
    publish_url = self.publish_eigentrust(id, scores, **kwargs)
    return scores, publish_url

  def run_and_publish_eigentrust(self, id: str, localtrust: List[IJV], pretrust: List[IV] = None, **kwargs) -> Tuple[List[Score], str]:
    overwrite = kwargs.get('overwrite', False)
    scores = self.run_eigentrust(localtrust, pretrust)
    publish_url = self.publish_eigentrust(id, scores, overwrite=overwrite)
    return scores, publish_url

  def publish_eigentrust(self, id: str, result: List[Score], **kwargs) -> str:
    overwrite = kwargs.get('overwrite', False)
    return self._upload_csv(result, SCORE_CSV_HEADERS, f'eigentrust/{id}', overwrite)

  def fetch_eigentrust(self, id: str, **kwargs) -> List[Score]:
    return self._convert_to_score(self._download_csv(f'eigentrust/{id}'))

  def publish_localtrust(self, id: str, result: List[IJV], **kwargs) -> str:
    overwrite = kwargs.get('overwrite', False)
    return self._upload_csv(result, IJV_CSV_HEADERS, f'localtrust/{id}', overwrite)

  def fetch_localtrust(self, id: str, **kwargs) -> List[IJV]:
    return self._convert_to_ijv(self._download_csv(f'localtrust/{id}'))

  def publish_pretrust(self, id: str, result: List[IV], **kwargs) -> str:
    overwrite = kwargs.get('overwrite', False)
    return self._upload_csv(result, IV_CSV_HEADERS, f'pretrust/{id}', overwrite)

  def fetch_pretrust(self, id: str, **kwargs) -> List[IV]:
    return self._convert_to_iv(self._download_csv(f'pretrust/{id}'))
