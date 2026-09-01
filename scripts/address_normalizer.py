import os
import time
import logging
import pandas as pd
import jellyfish

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def normalize_addresses(
    input_filepath: str, 
    output_filepath: str, 
    similarity_threshold: float = 0.90
) -> None:
    """
    배송지 주소 텍스트를 분석하여 유사한 주소를 동일한 문자열로 정규화합니다.
    대용량 처리 시 OOM 방지를 위해 청크 단위 스트리밍을 수행하며,
    연산 최적화를 위해 행정구역 단위의 블로킹 기법을 적용합니다.
    """
    # 입력 및 출력 파일 경로 동일 여부 검증 (덮어쓰기 방지)
    if os.path.abspath(input_filepath) == os.path.abspath(output_filepath):
        raise ValueError("입력 파일과 출력 파일의 경로가 같을 수 없습니다.")

    logger.info("주소 정규화 프로세스를 시작합니다 (OOM 방지 Chunk 처리 적용).")
    
    if not os.path.exists(input_filepath):
        logger.error(f"입력 파일을 찾을 수 없습니다: {input_filepath}")
        return

    start_time = time.time()
    
    # 청크 간 정규화 상태를 유지하기 위한 전역 캐시
    address_blocks = {}
    normalized_address_mapping = {}
    chunk_size = 10000

    try:
        # 대규모 데이터 처리를 위한 청크 분할 로드
        for chunk_idx, chunk in enumerate(pd.read_csv(input_filepath, chunksize=chunk_size)):
            unique_addresses = chunk['address'].dropna().astype(str).unique()
            
            for target_address in unique_addresses:
                # 기처리된 주소는 캐시를 활용하여 유사도 연산 생략
                if target_address in normalized_address_mapping:
                    continue
                    
                # 행정구역 단위(시/도, 시/군/구) 블로킹 알고리즘 적용
                address_parts = target_address.split()
                region_prefix = " ".join(address_parts[:2]) if len(address_parts) >= 2 else target_address
                
                if region_prefix not in address_blocks:
                    address_blocks[region_prefix] = []
                    
                reference_pool = address_blocks[region_prefix]
                
                best_match = target_address
                max_similarity_score = 0.0
                
                # Jaro-Winkler 유사도 검사
                for reference_address in reference_pool:
                    score = jellyfish.jaro_winkler_similarity(target_address, reference_address)
                    
                    # 유사도 임계값을 충족할 경우 기존 정규화 주소로 노드 병합
                    if score > max_similarity_score and score >= similarity_threshold:
                        max_similarity_score = score
                        best_match = reference_address

                # 매칭되는 레퍼런스가 없는 경우 신규 대표 주소로 풀에 등록
                if best_match == target_address:
                    reference_pool.append(best_match)
                
                # 정규화 결과 캐시 업데이트
                normalized_address_mapping[target_address] = best_match

            # 현재 청크 데이터에 정규화 결과 매핑
            chunk['normalized_address'] = chunk['address'].astype(str).map(normalized_address_mapping)
            
            # 첫 청크는 파일 생성 이후 청크는 이어쓰기 적용
            write_mode = 'w' if chunk_idx == 0 else 'a'
            write_header = True if chunk_idx == 0 else False
            
            chunk.to_csv(output_filepath, mode=write_mode, header=write_header, index=False, encoding='utf-8-sig')
            logger.info(f"청크 정규화 완료: 누적 {(chunk_idx + 1) * chunk_size:,} 건 처리됨")
            
    except Exception:
        logger.exception("데이터 청크 읽기 및 정규화 처리 중 예기치 않은 오류가 발생했습니다.")
        raise

    elapsed_time = time.time() - start_time
    logger.info(f"주소 정규화 처리 완료. 총 소요 시간: {elapsed_time:.2f}초")
    logger.info(f"정규화된 데이터 저장 완료: {output_filepath}")

if __name__ == "__main__":
    INPUT_FILE_PATH = 'mock_ticket_data.csv'
    OUTPUT_FILE_PATH = 'normalized_ticket_data.csv'
    
    try:
        normalize_addresses(INPUT_FILE_PATH, OUTPUT_FILE_PATH)
    except Exception as e:
        logger.error(f"주소 정규화 파이프라인 구동 실패: {e}")