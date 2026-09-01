import os
import time
import logging
import hashlib
import pandas as pd
from argon2 import PasswordHasher
from argon2.exceptions import HashingError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 아르곤2 운영 환경 기준 파라미터 상수화
ARGON2_MEMORY_COST = 65540
ARGON2_TIME_COST = 5
ARGON2_PARALLELISM = 2

def apply_data_hashing(
    input_filepath: str, 
    output_filepath: str, 
    columns_to_hash: list, 
    use_fast_mock_mode: bool = True
) -> None:
    """
    데이터프레임 내 지정된 컬럼의 데이터를 단방향 암호화하여 식별 불가능하도록 처리합니다.
    운영 환경에서는 아르곤2 알고리즘을 사용하며, 대규모 모의 데이터 생성 시에는 연산 속도 향상을 위해 SHA-256을 적용합니다.

    Args:
        input_filepath: 가공할 데이터가 포함된 원본 파일 경로
        output_filepath: 암호화가 완료된 데이터를 저장할 최종 파일 경로
        columns_to_hash: 비식별화 처리를 수행할 대상 컬럼명 리스트
        use_fast_mock_mode: 모의 데이터 생성을 위한 고속 해싱 적용 여부
    """
    # 입력 파일과 출력 파일 경로가 동일한 경우 덮어쓰기로 인한 데이터 증발 원천 차단
    if os.path.abspath(input_filepath) == os.path.abspath(output_filepath):
        raise ValueError("입력 파일과 출력 파일의 경로가 같을 수 없습니다.")

    logger.info("데이터 비식별화 및 해싱 프로세스를 시작합니다 (OOM 방지 Chunk 처리 적용).")
    
    if not os.path.exists(input_filepath):
        logger.error(f"입력 파일을 찾을 수 없습니다: {input_filepath}")
        return

    start_time = time.time()
    password_hasher = PasswordHasher(
        time_cost=ARGON2_TIME_COST, 
        memory_cost=ARGON2_MEMORY_COST, 
        parallelism=ARGON2_PARALLELISM
    )
    
    # Chunk 단위로 분할 처리하더라도 동일한 평문은 무조건 동일한 해시값을 갖도록 글로벌 매핑 딕셔너리 유지
    global_hash_mapping = {col: {} for col in columns_to_hash}
    chunk_size = 10000
    
    try:
        # 대용량 데이터로 인한 가비지 컬렉터 한계 및 메모리 초과 방지를 위해 1만 건씩 분할 로드
        for chunk_idx, chunk in enumerate(pd.read_csv(input_filepath, chunksize=chunk_size)):
            for column in columns_to_hash:
                unique_values = chunk[column].dropna().astype(str).unique()
                
                for raw_value in unique_values:
                    if raw_value not in global_hash_mapping[column]:
                        try:
                            if use_fast_mock_mode:
                                global_hash_mapping[column][raw_value] = hashlib.sha256(raw_value.encode('utf-8')).hexdigest()
                            else:
                                global_hash_mapping[column][raw_value] = password_hasher.hash(raw_value)
                        except HashingError as e:
                            logger.exception("Argon2 해싱 연산 중 치명적 오류가 발생했습니다.")
                            raise
                            
                # 글로벌 딕셔너리에서 매핑된 해시값을 현재 청크의 컬럼에 적용
                chunk[f'hashed_{column}'] = chunk[column].astype(str).map(global_hash_mapping[column])
                
            # 평문 민감 정보 및 불필요한 시스템 컬럼 제거
            columns_to_drop = ['name', 'phone', 'address', 'normalized_address', 'payment_method', 'ip']
            chunk = chunk.drop(columns=columns_to_drop, errors='ignore')
            
            # 첫 번째 청크는 파일을 새로 생성하고, 두 번째 청크부터는 데이터만 이어쓰기 모드 적용
            write_mode = 'w' if chunk_idx == 0 else 'a'
            write_header = True if chunk_idx == 0 else False
            
            chunk.to_csv(output_filepath, mode=write_mode, header=write_header, index=False, encoding='utf-8-sig')
            logger.info(f"청크 단위 병합 완료: 누적 {(chunk_idx + 1) * chunk_size:,} 건 처리됨")
            
    except Exception:
        logger.exception("데이터 청크 읽기 및 파일 저장 중 예기치 않은 오류가 발생했습니다.")
        raise

    elapsed_time = time.time() - start_time
    logger.info(f"데이터 암호화 처리 완료. 소요 시간: {elapsed_time:.2f}초")
    logger.info(f"최종 보안 데이터 저장 완료: {output_filepath}")

    # 암호화가 성공적으로 끝난 직후 평문 원본 CSV 파일을 시스템에서 영구 파기
    if os.path.exists(input_filepath):
        os.remove(input_filepath)
        logger.info(f"보안 규정 준수: 처리가 완료된 평문 입력 파일({input_filepath})을 시스템에서 즉시 파기했습니다.")

if __name__ == "__main__":
    INPUT_FILE = 'normalized_ticket_data.csv'
    OUTPUT_FILE = 'final_secure_ticket_data.csv'
    TARGET_COLUMNS = ['phone', 'payment_method', 'normalized_address']
    
    apply_data_hashing(
        input_filepath=INPUT_FILE, 
        output_filepath=OUTPUT_FILE, 
        columns_to_hash=TARGET_COLUMNS,
        use_fast_mock_mode=True
    )